# -*- coding: utf-8 -*-
"""Mock 提供方脚本推进测试（FR-07）。"""
import asyncio

from app.asr.mock import EDGE_SCRIPT, MockAsrProvider
from app.config import Settings


def run(script, packets):
    events = []

    async def emit(ev):
        events.append(ev)

    async def main():
        s = Settings(provider="mock", access_token="testtoken123")
        p = MockAsrProvider(s, script=script)
        q = asyncio.Queue()
        for pk in packets:
            q.put_nowait(pk)
        await p.run(q, emit)

    asyncio.run(main())
    return events


def test_script_progresses_with_audio_seconds():
    events = run([
        (0.5, ("transcript", {"text": "一", "sentence_id": 0, "sentence_type": 0})),
        (1.5, ("transcript", {"text": "一好", "sentence_id": 0, "sentence_type": 1})),
    ], [b"\x00" * 6400] * 10 + [None])   # 10 包 = 2s
    ts = [e for e in events if e.kind == "transcript"]
    assert [t.transcript["text"] for t in ts] == ["一", "一好"]
    assert [t.transcript["is_final"] for t in ts] == [False, True]
    assert events[0].kind == "status" and events[0].state == "listening"


def test_tail_fires_remaining_script_on_stop():
    # 只送 0.2s 音频就停止：停止时补完尾句（模拟尾句送达）
    events = run([
        (0.5, ("transcript", {"text": "尾句", "sentence_id": 0, "sentence_type": 1})),
    ], [b"\x00" * 6400, None])
    ts = [e for e in events if e.kind == "transcript"]
    assert ts and ts[-1].transcript["text"] == "尾句"


def test_edge_scenario_events():
    events = run(EDGE_SCRIPT, [b"\x00" * 6400] * 25 + [None])
    assert any(e.kind == "error" and e.recoverable for e in events)
    assert any(e.kind == "transcript" and e.transcript["speaker_id"] == 3
               for e in events)
