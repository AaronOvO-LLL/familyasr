# -*- coding: utf-8 -*-
"""会话生命周期（需求 FR-01 / FR-05 / FR-06）。

职责：
  * 驱动 ASR 提供方：start → connecting/listening → stopping → stopped/error；
  * 音频喂入与用量计账（按上游音频时长累计，静默上传也计入）；
  * 服务端硬配额：单次与每日限制，超限即正常结束并说明原因；
  * 单客户端占用：同一时刻只允许一个识别会话，第二个请求明确提示；
  * 停止后 3 秒尾句窗口，然后强制清理（不依赖客户端关闭事件）。
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from .asr.base import AsrEvent, EmitFn, ProviderStreamError
from .config import Settings
from .quota import QuotaStore

log = logging.getLogger("familyasr.session")

TERMINAL_STATES = ("stopped", "error")


class SessionManager:
    """单客户端并发控制：一次只允许一个上游识别会话。"""

    def __init__(self) -> None:
        self._current = None

    @property
    def busy(self) -> bool:
        return self._current is not None and not self._current.terminal

    def acquire(self, session) -> bool:
        if self.busy:
            return False
        self._current = session
        return True

    def release(self, session) -> None:
        if self._current is session:
            self._current = None


class LiveSession:
    """一次“开始字幕→停止”的完整生命周期（对应一个浏览器 WebSocket 连接内的会话）。"""

    def __init__(self, settings: Settings, quota: QuotaStore,
                 provider_factory, send) -> None:
        self.settings = settings
        self.quota = quota
        self._provider_factory = provider_factory
        self._send = send
        self.audio_q: asyncio.Queue = asyncio.Queue(maxsize=150)
        self.state = "idle"
        self.session_seconds = 0.0
        self.started_at = time.monotonic()
        self._task: asyncio.Task | None = None
        self._stop_requested = False

    # ---------------- 状态 ----------------
    @property
    def terminal(self) -> bool:
        return self._task is not None and self._task.done()

    def usage_message(self) -> dict:
        return {
            "type": "usage",
            "session_seconds": round(self.session_seconds, 1),
            "daily_seconds": round(self.quota.used_today(), 1),
            "daily_limit_seconds": self.settings.daily_limit_seconds,
            "session_limit_seconds": self.settings.session_limit_seconds,
        }

    async def _emit(self, ev: AsrEvent) -> None:
        if ev.kind == "transcript" and ev.transcript is not None:
            await self._send({"type": "transcript", "session_id": ev.session_id,
                              **ev.transcript})
        elif ev.kind == "status" and ev.state:
            self.state = ev.state
            await self._send({"type": "status", "state": ev.state,
                              "session_id": ev.session_id, "detail": ev.detail or ""})
        elif ev.kind == "error":
            await self._send({"type": "error", "code": ev.error_code or "upstream",
                              "message": ev.error_message or "",
                              "recoverable": ev.recoverable})

    # ---------------- 生命周期 ----------------
    async def start(self) -> None:
        self.started_at = time.monotonic()
        self.state = "connecting"
        await self._send({"type": "status", "state": "connecting", "session_id": None,
                          "detail": "正在连接语音服务"})
        self._task = asyncio.create_task(self._run(self._provider_factory()))

    async def _run(self, provider) -> None:
        terminal_state = "stopped"
        try:
            await provider.run(self.audio_q, self._emit)
        except ProviderStreamError as e:
            with contextlib.suppress(Exception):
                await self._send({"type": "error", "code": e.code,
                                  "message": e.message, "recoverable": False})
            terminal_state = "error"
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 —— 不向客户端暴露内部细节
            log.exception("session internal error")
            with contextlib.suppress(Exception):
                await self._send({"type": "error", "code": "internal",
                                  "message": "服务内部错误，请重新开始",
                                  "recoverable": False})
            terminal_state = "error"
        finally:
            self.quota.flush()
            self.state = terminal_state
            with contextlib.suppress(Exception):
                await self._send({"type": "status", "state": terminal_state,
                                  "session_id": None, "detail": ""})
                await self._send(self.usage_message())
            log.info("session end state=%s seconds=%.1f elapsed=%.1fs",
                     terminal_state, self.session_seconds,
                     time.monotonic() - self.started_at)

    def feed(self, pcm: bytes) -> str | None:
        """喂入 PCM；返回配额停止原因（None=正常）。"""
        if self._stop_requested or self.state not in (
                "connecting", "listening", "recovering"):
            return None
        sec = len(pcm) / (self.settings.sample_rate * 2.0)
        if self.session_seconds + sec > self.settings.session_limit_seconds:
            return "quota_session"
        if self.quota.used_today() + sec > self.settings.daily_limit_seconds:
            return "quota_daily"
        self.session_seconds += sec
        self.quota.add_seconds(sec)
        try:
            self.audio_q.put_nowait(pcm)
        except asyncio.QueueFull:
            pass   # 极端过载丢包（不重放、不排队堆积）
        return None

    async def stop(self, detail: str = "正在停止") -> None:
        """正常停止：停止采音→上游 end→限时尾句→清理。"""
        if self._task is None or self._task.done():
            return
        self._stop_requested = True
        self.state = "stopping"
        with contextlib.suppress(Exception):
            await self._send({"type": "status", "state": "stopping",
                              "session_id": None, "detail": detail})
        # 停止哨兵（保证送达：必要时等待队列腾挪）
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.audio_q.put(None), timeout=1.0)
        try:
            await asyncio.wait_for(asyncio.shield(self._task),
                                   timeout=self.settings.tail_timeout_seconds + 2.0)
        except TimeoutError:
            await self.abort()

    async def abort(self) -> None:
        """立即清理：取消提供方任务、释放上游连接（不等待尾句）。"""
        self._stop_requested = True
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        self.quota.flush()
