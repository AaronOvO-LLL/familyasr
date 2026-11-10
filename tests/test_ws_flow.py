# -*- coding: utf-8 -*-
"""端到端流程测试：鉴权、启停、字幕、单客户端、配额、心跳清理（FR-01/02/06/07）。

用 FastAPI TestClient 走真实 HTTP + WebSocket 栈，识别提供方为 mock
（不访问云、不产生费用）。真实腾讯云链路与 Pad 验收见阶段报告的依赖清单。
"""
import json
import queue
import threading
import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.config import Settings
from app.main import create_app

TOKEN = "testtoken123"


def make_settings(tmp_path, **kw) -> Settings:
    base = {
        "provider": "mock",
        "access_token": TOKEN,
        "usage_file": tmp_path / "usage.json",
    }
    base.update(kw)
    return Settings(**base)


def recv(ws, timeout=5.0):
    """带超时的 receive（starlette TestClient 无原生超时）。"""
    q: queue.Queue = queue.Queue()

    def _run():
        try:
            q.put(("msg", ws.receive()))
        except Exception as e:  # noqa: BLE001
            q.put(("err", e))

    threading.Thread(target=_run, daemon=True).start()
    try:
        kind, val = q.get(timeout=timeout)
    except queue.Empty:
        raise AssertionError(f"等待消息超时（{timeout}s）")
    if kind == "err":
        raise val
    return val


def recv_json(ws, timeout=5.0):
    msg = recv(ws, timeout)
    assert msg.get("type") == "websocket.send", msg
    return json.loads(msg["text"])


def recv_until(ws, pred, timeout=8.0, limit=50):
    """收消息直到 pred(msg) 为真；返回该消息。"""
    got = []
    for _ in range(limit):
        msg = recv_json(ws, timeout)
        got.append(msg)
        if pred(msg):
            return msg
    raise AssertionError(f"未等到目标消息，已收：{got}")


def pcm_packet(fill=0):
    return bytes([fill]) * 6400


# ---------------------------------------------------------------------------
# HTTP 与鉴权
# ---------------------------------------------------------------------------
def test_health_no_auth(tmp_path):
    client = TestClient(create_app(make_settings(tmp_path)))
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "provider": "mock"}
    assert TOKEN not in r.text


def test_page_requires_token_and_sets_cookie(tmp_path):
    client = TestClient(create_app(make_settings(tmp_path)))
    r = client.get("/")
    assert r.status_code == 401
    assert "访问码" in r.text
    r = client.get("/", params={"token": TOKEN})
    assert r.status_code == 200
    assert "家语字幕屏" in r.text
    assert "familyasr_token" in r.headers.get("set-cookie", "")
    # Cookie 后续可访问静态资源
    r2 = client.get("/static/app.js", cookies={"familyasr_token": TOKEN})
    assert r2.status_code == 200
    client.cookies.clear()          # httpx 会持久化 set-cookie，显式清除后再验无码访问
    r3 = client.get("/static/app.js")
    assert r3.status_code == 401


def test_wrong_token_rejected(tmp_path):
    client = TestClient(create_app(make_settings(tmp_path)))
    assert client.get("/", params={"token": "wrong-token"}).status_code == 401


def test_ws_auth_and_origin(tmp_path):
    client = TestClient(create_app(make_settings(tmp_path)))
    # 无访问码：accept 前被关闭（starlette 从 __enter__ 抛 WebSocketDisconnect）
    with pytest.raises(WebSocketDisconnect) as ei:
        with client.websocket_connect("/ws/live"):
            pass
    assert ei.value.code == 4401
    # 跨站 Origin
    with pytest.raises(WebSocketDisconnect) as ei:
        with client.websocket_connect(f"/ws/live?token={TOKEN}",
                                      headers={"origin": "http://evil.example"}):
            pass
    assert ei.value.code == 4403
    # 同源 Origin 放行
    with client.websocket_connect(f"/ws/live?token={TOKEN}",
                                  headers={"origin": "http://testserver"}) as ws:
        msg = recv_json(ws)
        assert msg["type"] == "status" and msg["state"] == "idle"


# ---------------------------------------------------------------------------
# 启停与字幕
# ---------------------------------------------------------------------------
def test_full_flow_start_transcript_stop(tmp_path):
    client = TestClient(create_app(make_settings(tmp_path)))
    with client.websocket_connect(f"/ws/live?token={TOKEN}") as ws:
        assert recv_json(ws)["state"] == "idle"
        ws.send_json({"type": "start", "client_session_id": "c1"})
        assert recv_until(ws, lambda m: m["type"] == "status"
                          and m["state"] == "connecting")
        assert recv_until(ws, lambda m: m["type"] == "status"
                          and m["state"] == "listening")
        # 5 秒音频：mock 脚本 4.2 秒前的句子都会触发
        for _ in range(25):
            ws.send_bytes(pcm_packet())
        finals = []
        interims = []
        for _ in range(20):
            m = recv_json(ws)
            if m["type"] == "transcript":
                (finals if m["is_final"] else interims).append(m)
                if any(t.get("sentence_id") == 2 and t["is_final"] for t in finals):
                    break
        assert interims, "应有临时句"
        assert any(t["sentence_id"] == 0 for t in interims)   # 同句修订归并前会多次出现
        ws.send_json({"type": "stop"})
        recv_until(ws, lambda m: m["type"] == "status" and m["state"] == "stopping")
        msg = recv_until(ws, lambda m: m["type"] == "status" and m["state"] == "stopped")
        assert msg is not None
        usage = recv_until(ws, lambda m: m["type"] == "usage")
        assert abs(usage["session_seconds"] - 5.0) < 0.3
        assert usage["daily_limit_seconds"] == 1800


def test_audio_before_start_rejected(tmp_path):
    client = TestClient(create_app(make_settings(tmp_path)))
    with client.websocket_connect(f"/ws/live?token={TOKEN}") as ws:
        recv_json(ws)
        ws.send_bytes(pcm_packet())
        m = recv_until(ws, lambda m: m["type"] == "error")
        assert m["code"] == "not_started"


def test_protocol_error_keeps_connection(tmp_path):
    client = TestClient(create_app(make_settings(tmp_path)))
    with client.websocket_connect(f"/ws/live?token={TOKEN}") as ws:
        recv_json(ws)
        ws.send_text("这不是 JSON")
        m = recv_until(ws, lambda m: m["type"] == "error")
        assert m["code"] == "protocol" and m["recoverable"] is True
        ws.send_json({"type": "ping"})
        assert recv_until(ws, lambda m: m["type"] == "pong")


def test_stop_without_start(tmp_path):
    client = TestClient(create_app(make_settings(tmp_path)))
    with client.websocket_connect(f"/ws/live?token={TOKEN}") as ws:
        recv_json(ws)
        ws.send_json({"type": "stop"})
        m = recv_until(ws, lambda m: m["type"] == "status")
        assert m["state"] == "stopped"


# ---------------------------------------------------------------------------
# 单客户端并发与清理
# ---------------------------------------------------------------------------
def test_second_client_busy(tmp_path):
    client = TestClient(create_app(make_settings(tmp_path)))
    with client.websocket_connect(f"/ws/live?token={TOKEN}") as ws1, \
         client.websocket_connect(f"/ws/live?token={TOKEN}") as ws2:
        recv_json(ws1)
        recv_json(ws2)
        ws1.send_json({"type": "start"})
        recv_until(ws1, lambda m: m["type"] == "status" and m["state"] == "listening")
        ws2.send_json({"type": "start"})
        m = recv_until(ws2, lambda m: m["type"] == "error")
        assert m["code"] == "busy"          # 明确提示，不偷偷抢占
        ws1.send_json({"type": "stop"})
        recv_until(ws1, lambda m: m["type"] == "status" and m["state"] == "stopped")


def test_disconnect_cleans_up_session(tmp_path):
    """关闭标签（断开 WS）后端及时清理，不留占用/计费会话。"""
    client = TestClient(create_app(make_settings(tmp_path)))
    with client.websocket_connect(f"/ws/live?token={TOKEN}") as ws1:
        recv_json(ws1)
        ws1.send_json({"type": "start"})
        recv_until(ws1, lambda m: m["type"] == "status" and m["state"] == "listening")
        ws1.send_bytes(pcm_packet())
    # 断开后新客户端应能正常开始（不再 busy）
    deadline = time.time() + 5
    ok = False
    while time.time() < deadline and not ok:
        with client.websocket_connect(f"/ws/live?token={TOKEN}") as ws2:
            recv_json(ws2)
            ws2.send_json({"type": "start"})
            m = recv_json(ws2)
            if m["type"] == "status" and m["state"] == "connecting":
                ok = True
            else:
                assert m.get("code") == "busy"
                time.sleep(0.3)
    assert ok, "断开后会话未清理（仍 busy）"


def test_idle_timeout_cleans_up(tmp_path):
    s = make_settings(tmp_path, client_idle_timeout_seconds=0.5)
    client = TestClient(create_app(s))
    with client.websocket_connect(f"/ws/live?token={TOKEN}") as ws:
        recv_json(ws)
        m = recv_until(ws, lambda m: m["type"] == "error", timeout=5.0)
        assert m["code"] == "idle_timeout"


# ---------------------------------------------------------------------------
# 配额（服务端硬限制）
# ---------------------------------------------------------------------------
def test_quota_daily_enforced(tmp_path):
    s = make_settings(tmp_path, daily_limit_seconds=0.5)
    client = TestClient(create_app(s))
    with client.websocket_connect(f"/ws/live?token={TOKEN}") as ws:
        recv_json(ws)
        ws.send_json({"type": "start"})
        recv_until(ws, lambda m: m["type"] == "status" and m["state"] == "listening")
        for _ in range(10):
            ws.send_bytes(pcm_packet())      # 每包 0.2s，总计 2s > 0.5s
        m = recv_until(ws, lambda m: m["type"] == "error")
        assert m["code"] == "quota_daily"
        recv_until(ws, lambda m: m["type"] == "status" and m["state"] == "stopped")


def test_quota_session_enforced(tmp_path):
    s = make_settings(tmp_path, session_limit_seconds=0.5)
    client = TestClient(create_app(s))
    with client.websocket_connect(f"/ws/live?token={TOKEN}") as ws:
        recv_json(ws)
        ws.send_json({"type": "start"})
        recv_until(ws, lambda m: m["type"] == "status" and m["state"] == "listening")
        for _ in range(10):
            ws.send_bytes(pcm_packet())
        m = recv_until(ws, lambda m: m["type"] == "error")
        assert m["code"] == "quota_session"
        recv_until(ws, lambda m: m["type"] == "status" and m["state"] == "stopped")


def test_quota_start_blocked_when_exhausted(tmp_path):
    # 0.4s 限额 = 恰好 2 个包（每包 0.2s），第 3 包起被拒，剩余额度为 0
    s = make_settings(tmp_path, daily_limit_seconds=0.4)
    client = TestClient(create_app(s))
    # 先消耗完当日额度
    with client.websocket_connect(f"/ws/live?token={TOKEN}") as ws:
        recv_json(ws)
        ws.send_json({"type": "start"})
        recv_until(ws, lambda m: m["type"] == "status" and m["state"] == "listening")
        for _ in range(10):
            ws.send_bytes(pcm_packet())
        recv_until(ws, lambda m: m["type"] == "error" and m["code"] == "quota_daily")
        recv_until(ws, lambda m: m["type"] == "status" and m["state"] == "stopped")
    # 新会话不允许开始（配额持久化，重启/重连不清零）
    # 断开清理存在时序：忙则短暂重试，最终必须拿到 quota_daily
    deadline = time.time() + 5
    codes = []
    while time.time() < deadline:
        with client.websocket_connect(f"/ws/live?token={TOKEN}") as ws:
            recv_json(ws)
            ws.send_json({"type": "start"})
            m = recv_until(ws, lambda m: m["type"] in ("error", "status"))
            if m["type"] == "status":
                raise AssertionError(f"配额耗尽后 start 竟被接受：{m}")
            if m["type"] == "error":
                codes.append(m["code"])
                assert m["code"] == "quota_daily"
                return
        time.sleep(0.3)
    raise AssertionError(f"未见 quota_daily 拒绝，收到：{codes}")


def test_usage_request(tmp_path):
    client = TestClient(create_app(make_settings(tmp_path)))
    with client.websocket_connect(f"/ws/live?token={TOKEN}") as ws:
        recv_json(ws)
        ws.send_json({"type": "usage"})
        m = recv_until(ws, lambda m: m["type"] == "usage")
        assert m["daily_seconds"] == 0
        assert m["session_seconds"] == 0


# ---------------------------------------------------------------------------
# V0.2 说话人标签（契约字段透传与显示开关）
# ---------------------------------------------------------------------------
def test_speaker_labels_flag_and_transcript_field(tmp_path):
    """初始 status 下发 speaker_labels；transcript 携带规范化的 speaker_id。"""
    client = TestClient(create_app(make_settings(tmp_path)))
    with client.websocket_connect(f"/ws/live?token={TOKEN}") as ws:
        m = recv_json(ws)
        assert m["speaker_labels"] is True
        ws.send_json({"type": "start", "client_session_id": "c1"})
        recv_until(ws, lambda m: m["type"] == "status" and m["state"] == "listening")
        for _ in range(25):
            ws.send_bytes(pcm_packet())
        t = recv_until(ws, lambda m: m["type"] == "transcript" and m.get("is_final"))
        assert t["speaker_id"] in (0, 1, 2, -1)   # 有效 0–9 或未知 -1
        ws.send_json({"type": "stop"})
        recv_until(ws, lambda m: m["type"] == "status" and m["state"] == "stopped")


def test_speaker_labels_disabled_flag(tmp_path):
    """SPEAKER_LABELS=off：仅隐藏标签（flag=False），不影响链路。"""
    client = TestClient(create_app(make_settings(tmp_path, speaker_labels=False)))
    with client.websocket_connect(f"/ws/live?token={TOKEN}") as ws:
        m = recv_json(ws)
        assert m["speaker_labels"] is False
