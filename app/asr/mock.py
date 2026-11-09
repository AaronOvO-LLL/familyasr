# -*- coding: utf-8 -*-
"""Mock 提供方（需求 FR-07）。

不访问云服务、不产生 ASR 费用、无需密钥。按“累计音频秒数”推进脚本，
可模拟：临时句→修订→定稿、空文本、重复、乱序、未知说话人、
可恢复错误、停止尾句等。仅用于功能与测试，不作为准确率/时延/费用证据。
"""
from __future__ import annotations

import asyncio
import uuid

from ..config import Settings
from .base import AsrEvent, EmitFn

# 脚本条目：(累计音频秒数, 动作)
#   ("transcript", dict(text, sentence_id, sentence_type, speaker_id?))
#   ("error", dict(code, message, recoverable))
#   ("status", dict(state, detail))
BASIC_SCRIPT = [
    (0.6, ("transcript", {"text": "今天", "sentence_id": 0, "sentence_type": 0})),
    (1.2, ("transcript", {"text": "今天天气", "sentence_id": 0, "sentence_type": 0})),
    (1.8, ("transcript", {"text": "今天天气不错。", "sentence_id": 0, "sentence_type": 1})),
    (2.6, ("transcript", {"text": "我们下午", "sentence_id": 1, "sentence_type": 0})),
    (3.2, ("transcript", {"text": "我们下午去买菜。", "sentence_id": 1, "sentence_type": 1,
                          "speaker_id": 1})),
    (4.2, ("transcript", {"text": "要记得带伞。", "sentence_id": 2, "sentence_type": 1,
                          "speaker_id": 2})),
]

# 边界情况脚本：覆盖空文本 / 重复定稿 / 乱序短文本 / 未知说话人 / 可恢复错误
EDGE_SCRIPT = [
    (0.5, ("transcript", {"text": "", "sentence_id": 0, "sentence_type": 0})),          # 空文本忽略
    (1.0, ("transcript", {"text": "好的", "sentence_id": 0, "sentence_type": 1})),
    (1.3, ("transcript", {"text": "好的呀", "sentence_id": 0, "sentence_type": 0})),     # 定稿后修订忽略
    (1.8, ("transcript", {"text": "明天早上八点", "sentence_id": 1, "sentence_type": 1,
                          "speaker_id": 3})),                                           # 未知说话人 3
    (2.2, ("error", {"code": "mock_transient", "message": "模拟网络抖动（可恢复）",
                     "recoverable": True})),
    (2.8, ("transcript", {"text": "明", "sentence_id": 1, "sentence_type": 0})),         # 乱序短文本忽略
    (3.4, ("transcript", {"text": "明天早上八点出发。", "sentence_id": 1, "sentence_type": 1,
                          "speaker_id": 3})),                                           # 同 ID 修订未定稿才生效——此处已定稿，忽略
    (4.0, ("transcript", {"text": "路上小心。", "sentence_id": 2, "sentence_type": 1})),
]

SCENARIOS = {"basic": BASIC_SCRIPT, "edge": EDGE_SCRIPT}


class MockAsrProvider:
    """按脚本回放识别事件；音频只计时长，不做识别。"""

    def __init__(self, settings: Settings, script=None, scenario: str = "basic",
                 packet_seconds: float | None = None) -> None:
        self.settings = settings
        if script is not None:
            self.script = list(script)
        else:
            self.script = list(SCENARIOS.get(scenario, BASIC_SCRIPT))
        self.session_id = uuid.uuid4().hex
        # 测试可注入更小的“每包秒数”，加速脚本推进
        self.packet_seconds = (packet_seconds if packet_seconds is not None
                               else settings.packet_ms / 1000.0)

    async def run(self, audio_q: asyncio.Queue, emit: EmitFn) -> None:
        await emit(AsrEvent(kind="status", state="listening", session_id=self.session_id,
                            detail="正在聆听（mock 模拟识别）"))
        audio_seconds = 0.0
        idx = 0

        async def fire(upto: float) -> None:
            nonlocal idx
            end_ms_default = int(audio_seconds * 1000)
            while idx < len(self.script) and self.script[idx][0] <= upto:
                _, (kind, payload) = self.script[idx]
                idx += 1
                if kind == "transcript":
                    end_ms = (end_ms_default if upto == float("inf")
                              else int(upto * 1000))
                    await emit(AsrEvent(kind="transcript", session_id=self.session_id,
                                        transcript={
                                            "sentence_id": payload.get("sentence_id", -1),
                                            "text": payload.get("text", ""),
                                            "is_final": payload.get("sentence_type", 0) == 1,
                                            "speaker_id": payload.get("speaker_id", -1),
                                            "start_time_ms": int(audio_seconds * 1000),
                                            "end_time_ms": end_ms,
                                        }))
                elif kind == "error":
                    await emit(AsrEvent(kind="error", session_id=self.session_id,
                                        error_code=payload.get("code", "mock_error"),
                                        error_message=payload.get("message", ""),
                                        recoverable=payload.get("recoverable", False)))
                elif kind == "status":
                    await emit(AsrEvent(kind="status", session_id=self.session_id,
                                        state=payload.get("state", "listening"),
                                        detail=payload.get("detail", "")))

        while True:
            item = await audio_q.get()
            if item is None:
                break
            audio_seconds += len(item) / (self.settings.sample_rate * 2.0)
            await fire(audio_seconds)
        # 停止：短尾句窗口（mock 立即补完未触发的脚本，模拟尾句送达）
        await asyncio.sleep(0.05)
        await fire(float("inf"))
