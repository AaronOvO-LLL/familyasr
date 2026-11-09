#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M0 最小实时验证工具（腾讯云实时语音识别 V2，WebSocket）。

用途（M0 可行性验证，仅测试用，不是产品应用）：
  把实时麦克风或 WAV 文件的 16kHz/16bit/单声道 PCM，按 1:1 实时速率
  经 wss 发送到腾讯云实时语音识别 V2，把识别事件打到控制台，
  供人工填写 docs/implementation/M0-结果记录表.md。

协议依据（2026-11-09 核对官方文档，实施时仍须以官方文档和实际回包为准）：
  * 请求地址  wss://asr.cloud.tencent.com/asr/v2/<appid>?<请求参数>
  * 鉴权      除 signature 外所有参数按字典序拼接
              "asr.cloud.tencent.com/asr/v2/<appid>?k1=v1&k2=v2..."
              作为签名原文，HMAC-SHA1(SecretKey) 后 Base64，再 urlencode
              以 signature= 参数拼回 URL。
  * 发送      binary 帧持续送 PCM；送完发文本 {"type":"end"}。
              建议每 200ms 送 200ms 音频（16k 一包 6400 字节），1:1 实时率；
              发送过快或间隔超 6 秒可能被断开。
  * 接收      JSON 文本帧：code/message/voice_id/message_id/sentences/final。
              sentences 为 {"sentence_list": [...]}，每项含
              sentence（文本）、sentence_id、sentence_type（0 不确定/1 确定）、
              speaker_id（-1 未知，0-9 话者）、start_time、end_time（毫秒）。
              final=1 表示整流结束（≠句子定稿；定稿看 sentence_type=1）。
  * voice_id  每次连接必须新生成（UUID），连接作废后不得复用。

证据边界（写进 M0 报告时必须保留）：
  * --input wav  = 验证“实时 V2 链路 + 识别效果”，不含麦克风采集环节。
  * --input mic  = Windows 电脑麦克风全链路；不能替代 Pad 收音验收。
  * 本工具输出只是测试记录素材，不保存音频、不保存识别文本到文件。

隐私与安全：
  * 凭据只从环境变量或 --env-file 指定的 .env 读取（ASR_APPID /
    ASR_SECRET_ID / ASR_SECRET_KEY），任何输出都不得出现 SecretKey、
    完整签名 URL 或完整 SecretId。
  * 不落盘任何音频与识别文字；结果由人工抄录进结果记录表。

用法示例：
  python scripts/m0_realtime_probe.py --input mic --seconds 120
  python scripts/m0_realtime_probe.py --input wav test.pcm.wav
  python scripts/m0_realtime_probe.py --dry-run          # 只检查配置与签名，不联网
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import queue
import random
import socket
import ssl
import struct
import sys
import threading
import time
import uuid
import wave
from urllib.parse import quote

# ---------------------------------------------------------------------------
# 常量（来自官方文档）
# ---------------------------------------------------------------------------
WS_HOST = "asr.cloud.tencent.com"
WS_PATH_PREFIX = "/asr/v2/"
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
TARGET_RATE = 16000          # 16kHz
SAMPLE_WIDTH = 2             # 16bit
CHANNELS = 1                 # 单声道
DEFAULT_PACKET_MS = 200      # 需求文档默认 200ms/包（16k = 6400 字节）
DEFAULT_ENGINE = "16k_zh_en_2.0"
KNOWN_ENGINES = ("16k_zh_en_2.0", "16k_zh_en_speaker_2.0")
TAIL_TIMEOUT_S = 5.0         # 停止后等待尾句/整流结束的上限（M0 工具取 5s）
END_GAP_WARN_S = 6.0         # 官方：包间隔超 6 秒可能出错

# 官方错误码 → 可读提示（不展示堆栈；文案面向测试人员）
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


def now_tag() -> str:
    return time.strftime("%H:%M:%S", time.localtime()) + f".{int(time.time() % 1 * 1000):03d}"


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


def build_ws_url(appid: str, secret_id: str, secret_key: str, engine_model_type: str,
                 packet_ms: int, nonce: int | None = None, timestamp: int | None = None,
                 extra_params: dict | None = None) -> tuple[str, dict]:
    """构造带签名的 wss URL。返回 (url, params)。voice_id 每次连接唯一。"""
    params = {
        "secretid": secret_id,
        "timestamp": int(time.time()) if timestamp is None else int(timestamp),
        "expired": (int(time.time()) if timestamp is None else int(timestamp)) + 3600,
        "nonce": random.randint(1, 999999999) if nonce is None else int(nonce),
        "engine_model_type": engine_model_type,
        "voice_id": uuid.uuid4().hex,   # 每次连接新生成，绝不复用
        "voice_format": 1,              # PCM
        "needvad": 1,
        "sentence_strategy": 0,         # 最小语义分句（需求建议值）
        "convert_num_mode": 1,
    }
    if extra_params:
        params.update(extra_params)
    # packet_ms 不是接口参数，只是本工具节流；此处不进入 params
    _ = packet_ms
    canonical = build_canonical(params, appid)
    signature = sign_canonical(canonical, secret_key)
    url = f"wss://{canonical}&signature={quote(signature, safe='')}"
    return url, params


def redact_secret_id(secret_id: str) -> str:
    if len(secret_id) <= 8:
        return secret_id[:2] + "****"
    return secret_id[:4] + "****" + secret_id[-2:]


# ---------------------------------------------------------------------------
# 识别结果解析与归并（纯函数 / 可单测）
# ---------------------------------------------------------------------------
def extract_sentence_items(message: dict) -> list[dict]:
    """从一条服务端消息里取出句子列表。

    真实腾讯云实时 V2 返回（2026-11-09 实测抓包，与官方文档示例不一致）：
      {"code":0, ..., "result": {"slice_type": 0|1|2, "index": N,
       "start_time": ms, "end_time": ms, "voice_text_str": "...", ...}}
      slice_type：0=句首块，1=修订中，2=定稿；index=句子序号（从 0 自增）。
    同时兼容文档旧形状：sentences 为 {"sentence_list": [...]} 或 [...]。
    """
    out = []

    def _item(sid, text, final, speaker, start, end):
        return {"sentence_id": sid, "sentence_type": 1 if final else 0,
                "speaker_id": speaker, "text": text,
                "start_time": start, "end_time": end}

    res = message.get("result")
    if isinstance(res, dict) and ("voice_text_str" in res or "slice_type" in res):
        speaker = -1
        si = res.get("speaker_info")
        if isinstance(si, dict):
            cand = si.get("speaker_id", si.get("id", -1))
            speaker = cand if isinstance(cand, int) else -1
        out.append(_item(
            sid=res.get("index", -1),
            text=res.get("voice_text_str", "") or "",
            final=res.get("slice_type") == 2,
            speaker=speaker,
            start=res.get("start_time"),
            end=res.get("end_time")))
        return out

    sentences = message.get("sentences")
    if isinstance(sentences, dict):
        items = sentences.get("sentence_list", [])
    elif isinstance(sentences, list):
        items = sentences
    else:
        items = []
    for s in items:
        if not isinstance(s, dict):
            continue
        out.append(_item(
            sid=s.get("sentence_id", -1),
            text=(s.get("sentence") if s.get("sentence") is not None else s.get("text", "")) or "",
            final=s.get("sentence_type", 0) == 1,
            speaker=s.get("speaker_id", -1),
            start=s.get("start_time"),
            end=s.get("end_time")))
    return out


class TranscriptTracker:
    """按 sentence_id 归并：临时句覆盖同 ID，定稿后冻结；重复/乱序/空文本容错。

    只属于一个连接（一个上游会话）；跨连接必须新建实例，
    旧连接迟到消息由调用方按 session/voice_id 隔离后才送达本类。
    """

    def __init__(self) -> None:
        self._items: dict[int, dict] = {}   # sentence_id -> 记录
        self._order: list[int] = []

    def update(self, item: dict) -> str:
        """返回事件类型：'interim' | 'final' | 'duplicate' | 'ignored'。"""
        sid = item.get("sentence_id")
        text = (item.get("text") or "").strip()
        if sid is None or sid < 0:
            return "ignored"
        if not text:
            return "ignored"
        cur = self._items.get(sid)
        is_final = item.get("sentence_type") == 1
        if cur is None:
            self._items[sid] = {**item, "text": text, "is_final": is_final}
            self._order.append(sid)
            return "final" if is_final else "interim"
        if cur["is_final"]:
            # 已定稿：迟到的重复/修订消息一律不再改动
            return "duplicate"
        if is_final or len(text) >= len(cur["text"]):
            cur.update(item)
            cur["text"] = text
            cur["is_final"] = is_final
            return "final" if is_final else "interim"
        return "ignored"   # 更短的乱序旧文本，丢弃

    def snapshot(self) -> list[dict]:
        return [self._items[s] for s in self._order]


# ---------------------------------------------------------------------------
# 音频：WAV 读取与重采样（纯函数 / 可单测）
# ---------------------------------------------------------------------------
def resample_pcm16(data: bytes, src_rate: int, dst_rate: int) -> bytes:
    """int16 小端 PCM 线性插值重采样。M0 够用；质量要求见测试方案的注意事项。"""
    if src_rate == dst_rate or not data:
        return data
    n_src = len(data) // 2
    samples = struct.unpack(f"<{n_src}h", data[: n_src * 2])
    n_dst = int(n_src * dst_rate / src_rate)
    out = []
    step = src_rate / dst_rate
    pos = 0.0
    for i in range(n_dst):
        idx = int(pos)
        frac = pos - idx
        a = samples[idx] if idx < n_src else samples[-1]
        b = samples[idx + 1] if idx + 1 < n_src else samples[-1]
        out.append(int(a + (b - a) * frac))
        pos += step
    return struct.pack(f"<{n_dst}h", *out)


class LinearResampler:
    """跨块连续的线性插值重采样（保持块边界处的相位连续）。"""

    def __init__(self, src_rate: int, dst_rate: int) -> None:
        self.src_rate = src_rate
        self.dst_rate = dst_rate
        self.step = src_rate / dst_rate
        self.pos = 0.0
        self._prev: int | None = None

    def process(self, data: bytes) -> bytes:
        if self.src_rate == self.dst_rate:
            return data
        n = len(data) // 2
        samples = struct.unpack(f"<{n}h", data[: n * 2])
        if not samples:
            return b""
        prev = self._prev if self._prev is not None else samples[0]
        self._prev = prev
        buf = (prev,) + samples
        out = []
        while self.pos < len(buf) - 1:
            idx = int(self.pos)
            frac = self.pos - idx
            a, b = buf[idx], buf[idx + 1]
            out.append(int(a + (b - a) * frac))
            self.pos += self.step
        self.pos -= len(samples)          # 保留帧与帧之间的相位
        self._prev = samples[-1]
        return struct.pack(f"<{len(out)}h", *out) if out else b""


def read_wav_as_pcm16k(path: str) -> tuple[bytes, float]:
    """读 WAV → 16k/16bit/单声道 PCM。返回 (pcm, 秒数)。立体声取双声道均值。"""
    with wave.open(path, "rb") as wf:
        rate = wf.getframerate()
        channels = wf.getnchannels()
        width = wf.getsampwidth()
        raw = wf.readframes(wf.getnframes())
    if width != 2:
        raise ValueError(f"仅支持 16bit WAV，当前 sampwidth={width}")
    if channels == 2:
        n = len(raw) // 2
        vals = struct.unpack(f"<{n}h", raw)
        mono = [(vals[i] + vals[i + 1]) // 2 for i in range(0, n, 2)]
        raw = struct.pack(f"<{len(mono)}h", *mono)
    elif channels != 1:
        raise ValueError(f"仅支持单/双声道，当前 channels={channels}")
    pcm = resample_pcm16(raw, rate, TARGET_RATE)
    return pcm, len(pcm) / (TARGET_RATE * SAMPLE_WIDTH)


def split_frames(pcm: bytes, frame_bytes: int) -> list[bytes]:
    return [pcm[i:i + frame_bytes] for i in range(0, len(pcm), frame_bytes)]


# ---------------------------------------------------------------------------
# 极简 WebSocket 客户端（仅标准库；客户端→服务端帧按规范掩码）
# ---------------------------------------------------------------------------
class MiniWebSocket:
    """仅支持 wss://（生产路径）；ws:// 明文仅用于本地回环自测。"""

    def __init__(self, url: str, timeout: float = 10.0) -> None:
        if url.startswith("wss://"):
            self._tls = True
        elif url.startswith("ws://"):
            self._tls = False
        else:
            raise ValueError("仅支持 wss://（测试可用 ws:// 连本地回环）")
        rest = url.split("://", 1)[1]
        hostport, _, path = rest.partition("/")
        host, _, port = hostport.partition(":")
        self.host = host
        self.port = int(port) if port else (443 if self._tls else 80)
        self.path = "/" + path
        self._buf = b""
        raw = socket.create_connection((self.host, self.port), timeout=timeout)
        if self._tls:
            self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=self.host)
        else:
            self.sock = raw
        self.sock.settimeout(timeout)
        self._handshake()

    def _handshake(self) -> None:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode("ascii"))
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("握手时连接被关闭")
            resp += chunk
        head, _, leftover = resp.partition(b"\r\n\r\n")
        self._buf = leftover
        status = head.split(b"\r\n", 1)[0]
        if b"101" not in status:
            raise ConnectionError(f"握手失败：{status.decode('utf-8', 'replace')}")
        accept_wanted = base64.b64encode(
            hashlib.sha1((key + WS_GUID).encode("ascii")).digest()
        ).decode("ascii")
        if accept_wanted.encode("ascii") not in head:
            raise ConnectionError("Sec-WebSocket-Accept 校验失败")

    def _recv_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("连接已关闭")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        header = bytes([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header += bytes([0x80 | n])
        elif n < 65536:
            header += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            header += bytes([0x80 | 127]) + struct.pack(">Q", n)
        self.sock.sendall(header + mask + masked)

    def send_text(self, text: str) -> None:
        self._send_frame(0x1, text.encode("utf-8"))

    def send_binary(self, data: bytes) -> None:
        self._send_frame(0x2, data)

    def recv(self, timeout: float | None = None) -> tuple[int, bytes] | None:
        """返回 (opcode, payload)；超时返回 None；连接关闭抛 ConnectionError。"""
        self.sock.settimeout(timeout)
        try:
            while True:
                b0 = self._recv_exact(2)
                opcode = b0[0] & 0x0F
                masked = bool(b0[1] & 0x80)
                length = b0[1] & 0x7F
                if length == 126:
                    length = struct.unpack(">H", self._recv_exact(2))[0]
                elif length == 127:
                    length = struct.unpack(">Q", self._recv_exact(8))[0]
                mask = self._recv_exact(4) if masked else b""
                payload = self._recv_exact(length) if length else b""
                if masked:
                    payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
                if opcode == 0x9:            # ping → pong
                    self._send_frame(0xA, payload)
                    continue
                if opcode == 0xA:            # pong
                    continue
                if opcode == 0x8:            # close
                    raise ConnectionError("服务端关闭连接")
                return opcode, payload
        except socket.timeout:
            return None

    def close(self) -> None:
        try:
            self._send_frame(0x8, b"")
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 麦克风音源（可选依赖 sounddevice，缺失时给出可操作提示）
# ---------------------------------------------------------------------------
class MicSource:
    """阻塞式读取固定时长 PCM 块；设备不支持 16k 时自动重采样并警告。"""

    def __init__(self, packet_ms: int, log) -> None:
        try:
            import sounddevice as sd  # 延迟导入：无麦克风需求时零依赖
        except ImportError as e:
            raise SystemExit(
                "麦克风模式需要安装 sounddevice（可选 numpy）：\n"
                "  python -m ensurepip && python -m pip install sounddevice\n"
                "（WAV 模式无需任何第三方依赖）"
            ) from e
        self._sd = sd
        self.packet_bytes = TARGET_RATE * SAMPLE_WIDTH * packet_ms // 1000
        self.block_samples = TARGET_RATE * packet_ms // 1000
        want_rate = TARGET_RATE
        try:
            self.stream = sd.RawInputStream(
                samplerate=want_rate, channels=1, dtype="int16",
                blocksize=self.block_samples)
            self.resampler = None
        except Exception:
            dev = sd.query_devices(kind="input")
            src_rate = int(dev["default_samplerate"])
            log(f"[警告] 设备不支持 16kHz，使用 {src_rate}Hz 采集后重采样（质量需人工评估）")
            self.stream = sd.RawInputStream(
                samplerate=src_rate, channels=1, dtype="int16",
                blocksize=int(src_rate * packet_ms / 1000))
            self.resampler = LinearResampler(src_rate, TARGET_RATE)
        self.stream.start()

    def frames(self):
        while True:
            data, _overflowed = self.stream.read(self.block_samples if self.resampler is None
                                                 else self.stream.blocksize)
            raw = bytes(data)
            if self.resampler is not None:
                raw = self.resampler.process(raw)
            yield raw

    def close(self) -> None:
        try:
            self.stream.stop()
            self.stream.close()
        except Exception:
            pass


class WavSource:
    """按 1:1 实时率回放 WAV 帧（用于链路/效果验证，不含麦克风环节）。"""

    def __init__(self, path: str, packet_ms: int) -> None:
        self.pcm, self.duration = read_wav_as_pcm16k(path)
        self.frame_bytes = TARGET_RATE * SAMPLE_WIDTH * packet_ms // 1000
        self._frames = split_frames(self.pcm, self.frame_bytes)

    def frames(self):
        next_send = time.monotonic()
        for fr in self._frames:
            wait = next_send - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            next_send += len(fr) / (TARGET_RATE * SAMPLE_WIDTH)
            yield fr

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def load_env_file(path: str) -> None:
    """把 KEY=VALUE 行并入环境变量（不回显值）。"""
    if not os.path.isfile(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


def get_credentials() -> tuple[str, str, str]:
    appid = os.environ.get("ASR_APPID", "").strip()
    secret_id = os.environ.get("ASR_SECRET_ID", "").strip()
    secret_key = os.environ.get("ASR_SECRET_KEY", "").strip()
    missing = [n for n, v in (("ASR_APPID", appid), ("ASR_SECRET_ID", secret_id),
                              ("ASR_SECRET_KEY", secret_key)) if not v]
    if missing:
        raise SystemExit(
            "缺少环境变量：" + ", ".join(missing) +
            "\n请通过环境或 --env-file .env 提供（参考 .env.example）。"
            "切勿把密钥写进仓库或聊天。")
    return appid, secret_id, secret_key


def run(args: argparse.Namespace) -> int:
    appid, secret_id, secret_key = get_credentials()
    log = lambda s: print(f"[{now_tag()}] {s}", flush=True)

    extra = {}
    if args.needvad is not None:
        extra["needvad"] = args.needvad
    if args.sentence_strategy is not None:
        extra["sentence_strategy"] = args.sentence_strategy
    url, params = build_ws_url(appid, secret_id, secret_key, args.engine,
                               args.packet_ms, extra_params=extra)
    log(f"引擎={args.engine} voice_format=1(PCM) needvad={params['needvad']} "
        f"sentence_strategy={params['sentence_strategy']} 包={args.packet_ms}ms "
        f"SecretId={redact_secret_id(secret_id)}")
    log(f"voice_id={params['voice_id']}（每次连接唯一，不复用）")
    if args.dry_run:
        log("[dry-run] 配置与签名构造正常，未发起连接。（URL 含签名，不打印）")
        return 0

    # 音源
    if args.input == "mic":
        if args.seconds is None:
            raise SystemExit("麦克风模式请用 --seconds 指定时长（建议 60–180 秒/段）")
        source: MicSource | WavSource = MicSource(args.packet_ms, log)
    else:
        source = WavSource(args.input, args.packet_ms)
        log(f"WAV：{args.input} 时长 {source.duration:.1f}s（按 1:1 实时率发送；"
            "此模式不含麦克风采集环节，证据边界见文档）")

    frame_bytes = TARGET_RATE * SAMPLE_WIDTH * args.packet_ms // 1000
    tracker = TranscriptTracker()
    events: queue.Queue = queue.Queue()
    stream_t0 = time.monotonic()

    def mark_thread():
        for line in sys.stdin:
            events.put(("mark", line.strip()))

    if args.mark:
        threading.Thread(target=mark_thread, daemon=True).start()

    try:
        ws = MiniWebSocket(url, timeout=10.0)
    except Exception as e:
        log(f"连接失败：{e}")
        return 2

    def recv_thread():
        while True:
            try:
                frame = ws.recv(timeout=1.0)
            except (ConnectionError, OSError) as e:
                events.put(("closed", str(e)))
                return
            if frame is None:
                continue
            opcode, payload = frame
            if opcode != 0x1:
                continue
            try:
                events.put(("msg", json.loads(payload.decode("utf-8"))))
            except (ValueError, UnicodeDecodeError):
                events.put(("badmsg", payload[:200]))

    threading.Thread(target=recv_thread, daemon=True).start()

    def drain(until_final: bool) -> bool:
        """处理事件队列；until_final 时等到 final=1 或尾句超时。"""
        deadline = time.monotonic() + (TAIL_TIMEOUT_S if until_final else 0)
        got_final = False
        while True:
            timeout = max(0.0, deadline - time.monotonic()) if until_final else 0.0
            try:
                kind, data = events.get(timeout=timeout if until_final else 0.05)
            except queue.Empty:
                if not until_final or time.monotonic() >= deadline:
                    return got_final
                continue
            if kind == "mark":
                log(f"[人工标记] {data}")
            elif kind == "closed":
                log(f"连接结束：{data}")
                return got_final
            elif kind == "badmsg":
                log(f"[警告] 无法解析的返回：{data!r}")
            elif kind == "msg":
                got_final = handle_message(data) or got_final
                if got_final and until_final:
                    return True

    def handle_message(msg: dict) -> bool:
        code = msg.get("code", 0)
        if code != 0:
            hint = ERROR_HINTS.get(code, msg.get("message", ""))
            log(f"服务端错误 code={code}：{hint}")
            return False
        if msg.get("final") == 1:
            log("整流结束（final=1）")
            return True
        for it in extract_sentence_items(msg):
            ev = tracker.update(it)
            span = ""
            if it["start_time"] is not None and it["end_time"] is not None:
                span = f"({it['start_time']}-{it['end_time']}ms"
                span += f", 说话人{it['speaker_id']})" if it["speaker_id"] >= 0 else ")"
            if ev == "interim":
                log(f"临时句 #{it['sentence_id']} {span}: {it['text']}")
            elif ev == "final":
                log(f"定稿   #{it['sentence_id']} {span}: {it['text']}")
            # duplicate / ignored 静默容错，不打扰测试记录
        return False

    # 发送循环（1:1 实时率；期间持续 drain 事件）
    sent_bytes = 0
    last_send = time.monotonic()
    stopped_reason = "音频发送完毕"
    try:
        for fr in source.frames():
            ws.send_binary(fr)
            sent_bytes += len(fr)
            drain(until_final=False)
            gap = time.monotonic() - last_send
            if gap > END_GAP_WARN_S:
                log(f"[警告] 发送间隔 {gap:.1f}s 超过官方 6 秒上限，连接可能被断开")
            last_send = time.monotonic()
            if args.seconds is not None and \
                    sent_bytes / (TARGET_RATE * SAMPLE_WIDTH) >= args.seconds:
                stopped_reason = f"达到设定时长 {args.seconds}s"
                break
    except KeyboardInterrupt:
        stopped_reason = "手动中断（Ctrl+C）"
    finally:
        source.close()

    log(f"{stopped_reason}；已发送 {sent_bytes / (TARGET_RATE * SAMPLE_WIDTH):.1f}s 音频。"
        "发送 {\"type\":\"end\"}，等待尾句……")
    try:
        ws.send_text(json.dumps({"type": "end"}))
    except OSError as e:
        log(f"发送 end 失败：{e}")
    got_final = drain(until_final=True)
    if not got_final:
        log(f"[提示] {TAIL_TIMEOUT_S:.0f} 秒内未收到 final=1；保留已显示文字，"
            "超出部分视为未定稿（不伪称定稿）。")

    finals = [s for s in tracker.snapshot() if s["is_final"]]
    interims = [s for s in tracker.snapshot() if not s["is_final"]]
    log(f"本次共 {len(tracker.snapshot())} 句：定稿 {len(finals)}，未定稿 {len(interims)}。")
    log("请把上方定稿文字按句抄入 docs/implementation/M0-结果记录表.md；"
        "本工具不保存任何音频与识别文本。")
    ws.close()
    return 0


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    p = argparse.ArgumentParser(description="M0 最小实时验证工具（腾讯云实时语音识别 V2）")
    p.add_argument("--input", default="mic",
                   help="mic（麦克风）或 WAV 文件路径")
    p.add_argument("--seconds", type=int, default=None, help="麦克风模式采集时长（秒）")
    p.add_argument("--engine", default=DEFAULT_ENGINE, choices=list(KNOWN_ENGINES))
    p.add_argument("--packet-ms", type=int, default=DEFAULT_PACKET_MS,
                   help="每包音频毫秒数（40–200，默认 200）")
    p.add_argument("--needvad", type=int, default=None, choices=[0, 1])
    p.add_argument("--sentence-strategy", type=int, default=None, choices=[0, 1])
    p.add_argument("--mark", action="store_true",
                   help="回车可插入人工标记（用于记录发声起点，测延迟）")
    p.add_argument("--env-file", default=".env", help="密钥环境文件（不入库）")
    p.add_argument("--dry-run", action="store_true", help="只校验配置与签名，不联网")
    args = p.parse_args()
    if args.input != "mic" and not os.path.isfile(args.input):
        raise SystemExit(f"找不到输入文件：{args.input}")
    if not 40 <= args.packet_ms <= 200:
        raise SystemExit("--packet-ms 允许范围 40–200")
    load_env_file(args.env_file)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
