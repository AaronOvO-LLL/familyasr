# -*- coding: utf-8 -*-
"""ASR 提供方抽象：统一事件模型，供 mock 与腾讯云实现。

事件（AsrEvent）由提供方在 run() 内通过 emit 回调异步发出：
  * status    —— 上游生命周期状态（connecting/listening/recovering）；
  * transcript—— 识别句（含 session_id=上游连接 ID，每次重连更新）；
  * error     —— 错误（recoverable=True 表示可重试类，已按策略处理或放弃）；
run() 正常返回 = 流正常结束（含尾句）；抛 ProviderStreamError = 不可继续。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Protocol


@dataclass
class AsrEvent:
    kind: str                       # "status" | "transcript" | "error"
    session_id: str | None = None   # 上游连接 ID（每次连接唯一）
    # status
    state: str | None = None
    detail: str = ""
    # transcript（字段与后端→前端消息一致）
    transcript: dict | None = None
    # error
    error_code: str | None = None
    error_message: str | None = None
    recoverable: bool = False


class ProviderStreamError(Exception):
    """上游不可继续（鉴权/欠费/参数/重试耗尽等）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


EmitFn = Callable[[AsrEvent], Awaitable[None]]
AudioQueue = "asyncio.Queue[bytes | None]"   # None = 停止信号（尾包后再发）


class AsrProvider(Protocol):
    async def run(self, audio_q: asyncio.Queue, emit: EmitFn) -> None:
        """消费 audio_q 中的 PCM（16k/16bit/mono），识别事件回调 emit。

        收到 None 表示音频结束：发送上游 end、等待尾句（限时），
        之后 run() 正常返回。取消（asyncio.CancelledError）时必须自行清理连接。
        """
        ...
