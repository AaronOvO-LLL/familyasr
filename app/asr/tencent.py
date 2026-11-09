# -*- coding: utf-8 -*-
"""腾讯云实时语音识别 V2（WebSocket）适配。

协议依据（2026-11-09 复核官方文档 product/1093/131127，实施仍以官方文档与实际回包为准）：
  * 请求地址  wss://asr.cloud.tencent.com/asr/v2/<appid>?<请求参数>
  * 鉴权      除 signature 外参数按字典序拼接
              "asr.cloud.tencent.com/asr/v2/<appid>?k1=v1&..." 为签名原文，
              HMAC-SHA1(SecretKey)→Base64→urlencode 拼回 signature 参数
  * 发送      binary 帧持续送 PCM（16k/16bit/mono）；送完发文本 {"type":"end"}
  * 接收      JSON 文本帧：code / message / voice_id / sentences / final
              sentence_type=1 句子定稿；final=1 整流结束（两者不混用）
  * voice_id  每次连接新生成，作废后不复用

重试策略（需求 FR-05）：临时网络故障/服务端 5000-5002 按 1s/2s/5s 最多三次；
同一中断周期累计，仅当连接稳定存活 60 秒后才清零；重试不重放旧音频。
鉴权失败、未开通、欠费、资源不足（4002-4005）等不循环重试。
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import logging
import random
import time
import uuid
from urllib.parse import quote

try:
    from websockets.asyncio.client import connect as _ws_connect  # websockets>=14
except ImportError:  # pragma: no cover - 旧版本回退
    from websockets import connect as _ws_connect

from ..config import Settings
from ..transcript import TranscriptTracker, extract_sentence_items
from .base import AsrEvent, EmitFn, ProviderStreamError

log = logging.getLogger("familyasr.tencent")

WS_HOST = "asr.cloud.tencent.com"
WS_PATH_PREFIX = "/asr/v2/"

# 官方错误码 → 可读提示（不展示堆栈；不回显密钥/签名）
ERROR_HINTS = {
    4000: "音频数据发送过快（1 秒内最多 3 秒音频）",
    4001: "参数不合法",
    4002: "鉴权失败（检查密钥与系统时间）",
    4003: "AppID 服务未开通",
    4004: "资源包耗尽（请开通后付费或购买资源包）",
    4005: "账户欠费停止服务",
    4006: "账号并发超限",
    4007: "音频解码失败（检查格式与参数一致性）",
    4008: "超过 15 秒未发送音频数据",
    4009: "连接已断开",
    4010: "上传了未知文本消息",
    5000: "服务端临时故障，可重新发起识别",
    5001: "服务端临时故障，可重新发起识别",
    5002: "服务端临时故障，可重新发起识别",
    6001: "境外调用受限（请检查网络出口与服务开通地区）",
}
# 可重试（瞬断）错误码；其余按不可恢复处理
TRANSIENT_CODES = {4009, 5000, 5001, 5002}
# 需求指定的统一提示（鉴权失败、服务未开通、余额或资源不足）
ADMIN_HINT = "语音服务暂不可用，请联系管理员"
ADMIN_HINT_CODES = {4002, 4003, 4004, 4005}


# ---------------------------------------------------------------------------
# 签名与 URL（纯函数，便于单测）
# ---------------------------------------------------------------------------
def build_canonical(params: dict, appid: str) -> str:
    """按官方规则拼签名原文：参数字典序，不含 signature，不含协议头。"""
    items = sorted((k, str(v)) for k, v in params.items() if k != "signature")
    query = "&".join(f"{k}={v}" for k, v in items)
    return f"{WS_HOST}{WS_PATH_PREFIX}{appid}?{query}"


def sign_canonical(canonical: str, secret_key: str) -> str:
    """HMAC-SHA1(SecretKey) 后 Base64，返回 signature 原值（未 urlencode）。"""
    digest = hmac.new(secret_key.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha1)
    return base64.b64encode(digest.digest()).decode("ascii")


def build_ws_url(settings: Settings, nonce: int | None = None,
                 timestamp: int | None = None) -> tuple[str, dict]:
    """构造带签名的 wss URL。voice_id 每次连接唯一。返回 (url, params)。"""
    ts = int(time.time()) if timestamp is None else int(timestamp)
    params = {
        "secretid": settings.secret_id,
        "timestamp": ts,
        "expired": ts + 3600,
        "nonce": random.randint(1, 999999999) if nonce is None else int(nonce),
        "engine_model_type": settings.engine_model_type,
        "voice_id": uuid.uuid4().hex,   # 每次连接新生成，绝不复用
        "voice_format": 1,              # PCM
        "needvad": 1,
        "sentence_strategy": 0,         # 最小语义分句（需求建议值）
        "convert_num_mode": 1,
    }
    canonical = build_canonical(params, settings.appid)
    signature = sign_canonical(canonical, settings.secret_key)
    url = f"wss://{canonical}&signature={quote(signature, safe='')}"
    return url, params


def classify_error(code: int, message: str = "") -> tuple[str, bool, str]:
    """返回 (可读提示, 是否瞬断可重试, 应用层错误码)。"""
    hint = ERROR_HINTS.get(code, message or "语音服务错误")
    transient = code in TRANSIENT_CODES
    if code in ADMIN_HINT_CODES:
        hint = ADMIN_HINT
    return hint, transient, f"upstream_{code}"


class _TransientUpstreamError(Exception):
    """网络/服务端瞬断：按重试策略处理。"""


async def _default_connect(url: str):
    return await _ws_connect(url, open_timeout=10, max_size=1 << 20,
                             ping_interval=20, ping_timeout=20)


class TencentAsrProvider:
    RETRY_DELAYS = (1.0, 2.0, 5.0)      # FR-05：1s/2s/5s 最多三次
    STABLE_RESET_S = 60.0               # 连接稳定存活后才清零重试计数

    def __init__(self, settings: Settings, connect_fn=None,
                 tail_timeout: float | None = None) -> None:
        self.settings = settings
        self._connect = connect_fn or _default_connect
        self.tail_timeout = (tail_timeout if tail_timeout is not None
                             else settings.tail_timeout_seconds)

    # ------------------------------------------------------------------
    async def run(self, audio_q: asyncio.Queue, emit: EmitFn) -> None:
        attempt = 0
        while True:
            session_id = uuid.uuid4().hex
            t0 = time.monotonic()
            try:
                await emit(AsrEvent(kind="status", state="connecting",
                                    session_id=session_id, detail="正在连接语音服务"))
                await self._one_connection(session_id, audio_q, emit)
                return
            except ProviderStreamError:
                raise
            except _TransientUpstreamError as e:
                if attempt >= len(self.RETRY_DELAYS):
                    raise ProviderStreamError(
                        "upstream_unavailable",
                        "语音服务暂不可用，请联系管理员") from e
                if time.monotonic() - t0 >= self.STABLE_RESET_S:
                    attempt = 0   # 短暂握手成功不累计清零
                delay = self.RETRY_DELAYS[attempt]
                attempt += 1
                await emit(AsrEvent(kind="status", state="recovering",
                                    session_id=session_id,
                                    detail=f"连接中断，{delay:.0f} 秒后自动重试"))
                await emit(AsrEvent(kind="error", session_id=session_id,
                                    error_code="upstream_interrupted",
                                    error_message="连接中断，部分内容可能遗漏",
                                    recoverable=True))
                await asyncio.sleep(delay)
                _drain_old_audio(audio_q)   # 重试不重放旧音频

    # ------------------------------------------------------------------
    async def _one_connection(self, session_id: str, audio_q: asyncio.Queue,
                              emit: EmitFn) -> None:
        url, _params = build_ws_url(self.settings)   # 新 voice_id；URL 含签名不外泄
        try:
            ws = await self._connect(url)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 —— DNS/拒连/握手超时等网络层失败按瞬断处理
            raise _TransientUpstreamError(f"连接失败：{e}") from e
        tracker = TranscriptTracker()
        up_q: asyncio.Queue = asyncio.Queue()
        final_evt = asyncio.Event()

        async def reader() -> None:
            try:
                async for raw in ws:
                    up_q.put_nowait(("msg", raw))
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 —— 网络层异常统一转瞬断
                up_q.put_nowait(("closed", e))
                return
            up_q.put_nowait(("closed", None))

        async def handler() -> None:
            handshake_done = False
            while True:
                kind, data = await up_q.get()
                if kind == "closed":
                    if final_evt.is_set():
                        return
                    raise _TransientUpstreamError("连接在整流结束前被关闭")
                try:
                    msg = json.loads(data)
                except (ValueError, UnicodeDecodeError):
                    continue   # 无法解析的帧容错忽略，不卡死
                code = msg.get("code", 0)
                if code != 0:
                    log.warning("tencent error code=%s message=%s",
                                code, str(msg.get("message", ""))[:200])
                    hint, transient, app_code = classify_error(
                        int(code), str(msg.get("message", "")))
                    if transient:
                        raise _TransientUpstreamError(hint)
                    raise ProviderStreamError(app_code, hint)
                if not handshake_done:
                    # 首条 code=0 消息即握手确认；此后才宣布“正在聆听”（FR-01）
                    handshake_done = True
                    await emit(AsrEvent(kind="status", state="listening",
                                        session_id=session_id, detail="正在聆听"))
                if msg.get("final") == 1:
                    final_evt.set()
                    return
                for it in extract_sentence_items(msg):
                    ev = tracker.update(it)
                    if ev in ("interim", "final"):
                        await emit(AsrEvent(kind="transcript", session_id=session_id,
                                            transcript={
                                                "sentence_id": it["sentence_id"],
                                                "text": it["text"],
                                                "is_final": ev == "final",
                                                "speaker_id": it["speaker_id"],
                                                "start_time_ms": it["start_time"],
                                                "end_time_ms": it["end_time"],
                                            }))

        rt = asyncio.create_task(reader())
        ht = asyncio.create_task(handler())
        get_audio: asyncio.Task | None = None
        try:
            while True:
                get_audio = asyncio.create_task(audio_q.get())
                await asyncio.wait({get_audio, ht}, return_when=asyncio.FIRST_COMPLETED)
                if ht.done():
                    get_audio.cancel()
                    await ht   # 正常返回 = 未发 end 就整流结束；异常=瞬断/致命
                    raise _TransientUpstreamError("上游提前结束整流")
                item = get_audio.result()
                if item is None:
                    break
                try:
                    await ws.send(item)
                except Exception as e:  # noqa: BLE001
                    raise _TransientUpstreamError(f"发送音频失败：{e}") from e
            # 音频结束：发 end，限时等尾句（FR-01 最多 3 秒）
            try:
                await ws.send(json.dumps({"type": "end"}))
            except Exception as e:  # noqa: BLE001
                raise _TransientUpstreamError(f"发送 end 失败：{e}") from e
            try:
                await asyncio.wait_for(ht, timeout=self.tail_timeout)
            except TimeoutError:
                pass   # 超时保留已有文字，未定稿部分不伪称定稿
            except (_TransientUpstreamError, ProviderStreamError):
                pass   # 尾句阶段出错不重试：保留已显示文字，正常收尾
        finally:
            rt.cancel()
            ht.cancel()
            if get_audio is not None:
                get_audio.cancel()
            with contextlib.suppress(Exception):
                await ws.close()


def _drain_old_audio(audio_q: asyncio.Queue) -> None:
    """丢弃排队中的旧音频包（不重放）；保留停止哨兵 None。"""
    saw_end = False
    while True:
        try:
            item = audio_q.get_nowait()
        except asyncio.QueueEmpty:
            break
        if item is None:
            saw_end = True
    if saw_end:
        audio_q.put_nowait(None)
