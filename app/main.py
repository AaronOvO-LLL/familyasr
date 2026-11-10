# -*- coding: utf-8 -*-
"""家语字幕屏 M1 服务入口（FastAPI）。

HTTP：GET / 页面（需访问码）、GET /health 非敏感健康状态、静态资源；
WebSocket：/ws/live —— start/stop 控制 + 二进制 PCM + transcript/status/error/usage。
详细消息契约见 docs/implementation/M1-接口约定.md。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, Response

from . import auth
from .asr import create_provider
from .config import ROOT, Settings
from .quota import QuotaStore
from .session import LiveSession, SessionManager

log = logging.getLogger("familyasr")

STATIC_DIR = ROOT / "static"

UNAUTHORIZED_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>家语字幕屏｜需要访问码</title>
<style>body{background:#111;color:#eee;font-family:system-ui;
display:flex;min-height:100vh;align-items:center;justify-content:center}
form{background:#1c1c1e;padding:2rem 2.5rem;border-radius:12px}
input,button{font-size:1.25rem;padding:.6rem 1rem;margin:.4rem 0}
button{background:#2e7d32;color:#fff;border:0;border-radius:8px}</style>
</head><body>
<form method="get" action="/">
  <h1>家语字幕屏</h1>
  <p>请输入家庭访问码（由管理员提供）：</p>
  <input name="token" type="password" autocomplete="current-password" required>
  <button type="submit">进入</button>
</form>
</body></html>"""


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    problems = settings.validate()
    if problems:
        raise RuntimeError("配置无效：" + "；".join(problems))

    app = FastAPI(title="家语字幕屏", docs_url=None, redoc_url=None, openapi_url=None)
    quota = QuotaStore(settings.usage_file, settings.daily_limit_seconds)
    manager = SessionManager()
    packet_seconds = settings.packet_ms / 1000.0   # 一包音频的时长（配额下限）

    def _set_token_cookie(response: Response, token: str) -> None:
        # HttpOnly：JS 不需要读取（WebSocket 握手自动携带）
        response.set_cookie(
            auth.COOKIE_NAME, token, httponly=True, samesite="lax",
            max_age=30 * 24 * 3600, path="/")

    def _cookie_token(request: Request) -> str | None:
        return auth.extract_token_from_cookie(request.headers.get("cookie"))

    # ----------------- HTTP -----------------
    @app.get("/")
    async def index(request: Request, token: str | None = None):
        provided = token or _cookie_token(request)
        if not auth.token_ok(provided, settings.access_token):
            return HTMLResponse(UNAUTHORIZED_HTML, status_code=401)
        resp = FileResponse(STATIC_DIR / "index.html", media_type="text/html")
        if token and auth.token_ok(token, settings.access_token):
            _set_token_cookie(resp, token)
        return resp

    @app.get("/health")
    async def health():
        # 非敏感健康状态：不含密钥、访问码、用量明细
        return {"status": "ok", "provider": settings.provider}

    @app.get("/static/{path:path}")
    async def static_files(path: str, request: Request):
        if not auth.token_ok(_cookie_token(request), settings.access_token):
            return HTMLResponse(UNAUTHORIZED_HTML, status_code=401)
        target = (STATIC_DIR / path).resolve()
        if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.is_file():
            return HTMLResponse("not found", status_code=404)
        return FileResponse(target)

    # ----------------- WebSocket -----------------
    @app.websocket("/ws/live")
    async def ws_live(ws: WebSocket):
        token = auth.extract_token(ws.scope)
        if not auth.token_ok(token, settings.access_token):
            await ws.close(code=4401)
            return
        origin = _header(ws, "origin")
        host = _header(ws, "host")
        if not auth.origin_ok(origin, host):
            await ws.close(code=4403)
            return
        await ws.accept()

        out_q: asyncio.Queue = asyncio.Queue()
        session: LiveSession | None = None

        async def send(msg: dict) -> None:
            await out_q.put(msg)

        async def sender() -> None:
            while True:
                msg = await out_q.get()
                if msg is None:
                    return
                with contextlib.suppress(Exception):
                    await ws.send_text(json.dumps(msg, ensure_ascii=False))

        inbound: asyncio.Queue = asyncio.Queue()

        async def receiver() -> None:
            try:
                while True:
                    msg = await ws.receive()
                    inbound.put_nowait(msg)
                    if msg.get("type") == "websocket.disconnect":
                        return
            except WebSocketDisconnect:
                inbound.put_nowait({"type": "websocket.disconnect"})
            except Exception:  # noqa: BLE001
                inbound.put_nowait({"type": "websocket.disconnect"})

        sender_task = asyncio.create_task(sender())
        receiver_task = asyncio.create_task(receiver())
        last_client = asyncio.get_running_loop().time()

        async def handle_text(raw: str) -> bool:
            """返回 False 表示应结束连接。"""
            nonlocal session
            try:
                data = json.loads(raw)
                mtype = data.get("type")
            except (ValueError, TypeError):
                await send({"type": "error", "code": "protocol",
                            "message": "无法识别的消息", "recoverable": True})
                return True
            if mtype == "ping":
                await send({"type": "pong"})
            elif mtype == "usage":
                if session is not None:
                    await send(session.usage_message())
                else:
                    await send({"type": "usage", "session_seconds": 0,
                                "daily_seconds": round(quota.used_today(), 1),
                                "daily_limit_seconds": settings.daily_limit_seconds,
                                "session_limit_seconds": settings.session_limit_seconds})
            elif mtype == "start":
                if session is not None and not session.terminal:
                    await send({"type": "error", "code": "busy",
                                "message": "已有会话进行中，请先停止", "recoverable": True})
                elif manager.busy:
                    await send({"type": "error", "code": "busy",
                                "message": "另一台设备正在使用，请稍后再试",
                                "recoverable": True})
                elif quota.remaining_daily() < packet_seconds:
                    # 剩余额度不足一包（200ms）：直接拒绝，避免“开始即停”
                    await send({"type": "error", "code": "quota_daily",
                                "message": "今日用量已达上限，明天再来", "recoverable": False})
                else:
                    if session is not None:
                        manager.release(session)
                    session = LiveSession(settings, quota,
                                          lambda: create_provider(settings), send)
                    manager.acquire(session)
                    await session.start()
            elif mtype == "stop":
                if session is not None:
                    await session.stop()
                else:
                    await send({"type": "status", "state": "stopped",
                                "session_id": None, "detail": ""})
            else:
                await send({"type": "error", "code": "protocol",
                            "message": f"未知消息类型：{mtype!r}", "recoverable": True})
            return True

        try:
            await send({"type": "status", "state": "idle", "session_id": None,
                        "detail": "连接就绪"})
            while True:
                try:
                    msg = await asyncio.wait_for(inbound.get(), timeout=1.0)
                except (TimeoutError, asyncio.TimeoutError):
                    now = asyncio.get_running_loop().time()
                    if now - last_client > settings.client_idle_timeout_seconds:
                        await send({"type": "error", "code": "idle_timeout",
                                    "message": "长时间无活动，已自动停止并清理",
                                    "recoverable": False})
                        break
                    continue
                if msg.get("type") == "websocket.disconnect":
                    break
                if msg.get("type") != "websocket.receive":
                    continue
                last_client = asyncio.get_running_loop().time()
                data = msg.get("bytes")
                if data is not None:
                    if session is None:
                        await send({"type": "error", "code": "not_started",
                                    "message": "请先点击“开始字幕”", "recoverable": True})
                        continue
                    reason = session.feed(data)
                    if reason == "quota_daily":
                        await send({"type": "error", "code": "quota_daily",
                                    "message": "已达今日用量上限，字幕已停止",
                                    "recoverable": False})
                        await session.stop("已达今日用量上限")
                    elif reason == "quota_session":
                        await send({"type": "error", "code": "quota_session",
                                    "message": "已达单次使用上限，字幕已停止",
                                    "recoverable": False})
                        await session.stop("已达单次使用上限")
                    continue
                text = msg.get("text")
                if text is not None:
                    await handle_text(text)
        except WebSocketDisconnect:
            pass
        except Exception:  # noqa: BLE001 —— 不向客户端暴露内部细节
            log.exception("ws loop error")
        finally:
            if session is not None:
                await session.abort()   # 断开/超时立即清理，不留下计费会话
                manager.release(session)
            out_q.put_nowait(None)
            receiver_task.cancel()
            with contextlib.suppress(Exception):
                await sender_task
            quota.flush()
            log.info("ws closed")

    return app


def _header(ws: WebSocket, name: str) -> str | None:
    for k, v in ws.scope.get("headers", []):
        if k.lower() == name.encode():
            return v.decode("latin-1")
    return None
