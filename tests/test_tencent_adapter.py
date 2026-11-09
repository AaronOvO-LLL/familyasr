# -*- coding: utf-8 -*-
"""腾讯云适配层测试：签名、错误分类、重试策略、无音频重放（FR-05）。

上游连接用假 WebSocket 注入，不发起真实网络调用（真实链路验证需凭据，
见 docs/reports/M1-实施报告.md 的依赖清单）。
"""
import asyncio
import json

import pytest

from app.asr.tencent import (ADMIN_HINT, TencentAsrProvider, _drain_old_audio,
                             build_canonical, build_ws_url, classify_error,
                             sign_canonical)
from app.config import Settings

# 独立工具（openssl）预先计算的签名黄金值：
#   printf '%s' '<canonical>' | openssl dgst -sha1 -hmac 'testkey' -binary | openssl base64 -A
GOLDEN_CANONICAL = ("asr.cloud.tencent.com/asr/v2/12345?convert_num_mode=1"
                    "&engine_model_type=16k_zh_en_2.0&expired=1700003600&needvad=1"
                    "&nonce=42&secretid=testid&sentence_strategy=0"
                    "&timestamp=1700000000&voice_format=1&voice_id=abc")
GOLDEN_SIGNATURE = "khLRnSbX9Qix1SBKJMUGuZ5eTjg="


def make_settings(**kw) -> Settings:
    base = dict(provider="tencent", appid="12345", secret_id="testid",
                secret_key="testkey", access_token="testtoken123")
    base.update(kw)
    return Settings(**base)


# ---------------------------------------------------------------------------
# 签名
# ---------------------------------------------------------------------------
def test_sign_golden_vector():
    assert sign_canonical(GOLDEN_CANONICAL, "testkey") == GOLDEN_SIGNATURE


def test_build_canonical_sorts_params():
    params = {"b": 2, "a": 1, "signature": "x"}
    assert build_canonical(params, "12345") == \
        "asr.cloud.tencent.com/asr/v2/12345?a=1&b=2"


def test_build_ws_url_shape_and_unique_voice_id():
    s = make_settings()
    url1, p1 = build_ws_url(s, nonce=1, timestamp=1700000000)
    url2, p2 = build_ws_url(s, nonce=1, timestamp=1700000000)
    assert url1.startswith("wss://asr.cloud.tencent.com/asr/v2/12345?")
    assert "signature=" in url1
    assert p1["voice_id"] != p2["voice_id"]          # 每次连接新 voice_id
    assert p1["voice_format"] == 1 and p1["needvad"] == 1
    assert p1["sentence_strategy"] == 0
    # URL 中的签名必须与 URL 自身参数自洽（排序 + urlencode）
    from urllib.parse import parse_qs, urlparse
    qs = parse_qs(urlparse(url1).query)
    sig = qs.pop("signature")[0]
    flat = {k: v[0] for k, v in qs.items()}
    canonical = build_canonical(flat, "12345")
    assert sign_canonical(canonical, "testkey") == sig


# ---------------------------------------------------------------------------
# 错误分类
# ---------------------------------------------------------------------------
def test_classify_error_transient_and_fatal():
    hint, transient, code = classify_error(5001, "")
    assert transient and code == "upstream_5001"
    hint, transient, code = classify_error(4002, "")
    assert not transient and hint == ADMIN_HINT and code == "upstream_4002"
    hint, transient, code = classify_error(4007, "")
    assert not transient and "音频解码" in hint
    hint, transient, _ = classify_error(9999, "自定义")
    assert not transient and hint == "自定义"


# ---------------------------------------------------------------------------
# 假 WebSocket 注入
# ---------------------------------------------------------------------------
class FakeWs:
    """脚本化上游连接。incoming 为空后默认挂起（模拟长连接），不再断开。"""

    def __init__(self, incoming=(), fail_on_send=None):
        self.incoming = list(incoming)
        self.fail_on_send = fail_on_send
        self.sent = []
        self.closed = False

    async def send(self, data):
        if self.fail_on_send is not None and len(self.sent) + 1 >= self.fail_on_send:
            raise OSError("simulated disconnect")
        self.sent.append(data)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.incoming:
            await asyncio.sleep(0.005)   # 节奏，避免与发送循环竞态
            return self.incoming.pop(0)
        await asyncio.Event().wait()
        raise StopAsyncIteration

    async def close(self):
        self.closed = True


class FakeConnect:
    def __init__(self, plan):
        """plan: 异常或 FakeWs 的列表，逐次消费；耗尽后再调用即报错（防止死等）。"""
        self.plan = list(plan)
        self.calls = []

    async def __call__(self, url):
        self.calls.append(url)
        if not self.plan:
            raise AssertionError("FakeConnect: 连接次数超出预期")
        item = self.plan.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


ACK = json.dumps({"code": 0, "message": "success"})
FINAL_END = json.dumps({"code": 0, "final": 1})


def transcripts(events):
    return [e for e in events if e.kind == "transcript"]


def run_provider(provider, packets):
    """预置音频包（None=停止）后跑完 run()，收集事件。"""
    events = []

    async def emit(ev):
        events.append(ev)

    async def main():
        q = asyncio.Queue()
        for p in packets:
            q.put_nowait(p)
        await provider.run(q, emit)
        return q

    asyncio.run(main())
    return events


def test_normal_flow_emits_transcripts():
    ws = FakeWs(incoming=[
        ACK,
        json.dumps({"sentences": {"sentence_list": [
            {"sentence": "今天", "sentence_id": 0, "sentence_type": 0}]}}),
        json.dumps({"sentences": {"sentence_list": [
            {"sentence": "今天天气不错。", "sentence_id": 0, "sentence_type": 1}]}}),
        FINAL_END,
    ])
    provider = TencentAsrProvider(make_settings(), connect_fn=FakeConnect([ws]))
    events = run_provider(provider, [b"\x00" * 6400, b"\x00" * 6400, None])
    kinds = [e.kind for e in events]
    assert kinds.count("status") >= 2          # connecting + listening
    ts = transcripts(events)
    assert [t.transcript["text"] for t in ts] == ["今天", "今天天气不错。"]
    assert [t.transcript["is_final"] for t in ts] == [False, True]
    assert json.loads(ws.sent[-1]) == {"type": "end"}   # 停止发 end
    assert ws.sent[:-1] == [b"\x00" * 6400, b"\x00" * 6400]


def test_fatal_auth_error_no_retry():
    ws = FakeWs(incoming=[ACK, json.dumps({"code": 4002, "message": "auth"})])
    fc = FakeConnect([ws])
    provider = TencentAsrProvider(make_settings(), connect_fn=fc)
    with pytest.raises(Exception) as ei:
        run_provider(provider, [b"\x00" * 6400])
    assert getattr(ei.value, "code", "") == "upstream_4002"
    assert ADMIN_HINT in str(ei.value)
    assert len(fc.calls) == 1                   # 鉴权失败不循环重试


def test_retry_on_connect_failure_then_success():
    ok_ws = FakeWs(incoming=[ACK, FINAL_END])
    fc = FakeConnect([ConnectionRefusedError("refused"), OSError("net"), ok_ws])
    provider = TencentAsrProvider(make_settings(), connect_fn=fc)
    provider.RETRY_DELAYS = (0.01, 0.02, 0.05)
    events = run_provider(provider, [b"\x00" * 6400, None])
    assert len(fc.calls) == 3
    assert any(e.kind == "status" and e.state == "recovering" for e in events)
    assert any(e.kind == "error" and e.recoverable for e in events)
    assert any(e.kind == "status" and e.state == "listening" for e in events)


def test_retry_exhausted_raises_unavailable():
    fc = FakeConnect([ConnectionRefusedError("refused") for _ in range(4)])
    provider = TencentAsrProvider(make_settings(), connect_fn=fc)
    provider.RETRY_DELAYS = (0.01, 0.02, 0.05)
    with pytest.raises(Exception) as ei:
        run_provider(provider, [b"\x00" * 6400])
    assert getattr(ei.value, "code", "") == "upstream_unavailable"
    assert len(fc.calls) == 4                   # 1 次初始 + 3 次重试


def test_no_audio_replay_and_new_voice_id():
    """中途断开：重试不重放旧音频，且每次连接新 voice_id。"""
    ws1 = FakeWs(incoming=[ACK], fail_on_send=2)      # 第 2 包发送时断开
    ws2 = FakeWs(incoming=[ACK, FINAL_END])
    fc = FakeConnect([ws1, ws2])
    provider = TencentAsrProvider(make_settings(), connect_fn=fc)
    provider.RETRY_DELAYS = (0.01, 0.02, 0.05)
    packets = [bytes([i]) * 6400 for i in range(6)] + [None]
    run_provider(provider, packets)
    # 第二连接只收到 end，不含任何重放音频
    assert len(ws2.sent) == 1
    assert json.loads(ws2.sent[0]) == {"type": "end"}
    # 两次连接 URL 的 voice_id 不同
    from urllib.parse import parse_qs, urlparse
    v1 = parse_qs(urlparse(fc.calls[0]).query)["voice_id"][0]
    v2 = parse_qs(urlparse(fc.calls[1]).query)["voice_id"][0]
    assert v1 != v2


def test_drain_keeps_end_sentinel():
    q = asyncio.Queue()
    for _ in range(3):
        q.put_nowait(b"x")
    q.put_nowait(None)
    q.put_nowait(b"y")

    async def main():
        _drain_old_audio(q)
        items = []
        while not q.empty():
            items.append(q.get_nowait())
        return items

    assert asyncio.run(main()) == [None]


def test_tail_timeout_keeps_text():
    """end 后上游一直不回 final：超时正常返回，不抛错（保留已有文字）。"""
    ws = FakeWs(incoming=[
        ACK,
        json.dumps({"sentences": {"sentence_list": [
            {"sentence": "未定稿", "sentence_id": 0, "sentence_type": 0}]}}),
    ])
    provider = TencentAsrProvider(make_settings(), connect_fn=FakeConnect([ws]),
                                  tail_timeout=0.05)
    events = run_provider(provider, [b"\x00" * 6400, None])
    ts = transcripts(events)
    assert ts and ts[-1].transcript["is_final"] is False
    assert json.loads(ws.sent[-1]) == {"type": "end"}
