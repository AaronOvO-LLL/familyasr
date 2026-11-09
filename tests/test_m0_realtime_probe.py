# -*- coding: utf-8 -*-
"""m0_realtime_probe.py 核心逻辑单测（仅标准库，无网络、无第三方依赖）。

覆盖：签名/URL 构造、识别结果解析与归并、重采样与 WAV 读取、
WebSocket 客户端帧处理（本地回环服务端验证握手/掩码/ping/关闭）。
运行：python -m unittest discover -s tests -v
"""

import base64
import hashlib
import hmac
import importlib.util
import json
import pathlib
import re
import socket
import struct
import threading
import unittest
import wave
from urllib.parse import urlparse, parse_qs

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "m0_realtime_probe", ROOT / "scripts" / "m0_realtime_probe.py")
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)


# ---------------------------------------------------------------------------
# 签名与 URL
# ---------------------------------------------------------------------------
class TestSignature(unittest.TestCase):
    def test_canonical_sorted_no_spaces(self):
        params = {"secretid": "S", "timestamp": 2, "nonce": 1, "engine_model_type": "e",
                  "expired": 3, "signature": "X"}
        canonical = probe.build_canonical(params, "12345")
        self.assertEqual(
            canonical,
            "asr.cloud.tencent.com/asr/v2/12345"
            "?engine_model_type=e&expired=3&nonce=1&secretid=S&timestamp=2")
        self.assertNotIn(" ", canonical)          # 官方签名原文不含空格
        self.assertNotIn("signature", canonical)  # signature 本身不参与签名

    def test_signature_is_b64_hmac_sha1(self):
        canonical = "asr.cloud.tencent.com/asr/v2/1?x=1"
        key = "test-secret-key"
        expect = base64.b64encode(
            hmac.new(key.encode(), canonical.encode(), hashlib.sha1).digest()).decode()
        self.assertEqual(probe.sign_canonical(canonical, key), expect)

    def test_build_ws_url_signature_verifiable(self):
        url, params = probe.build_ws_url(
            "12345", "secret-id", "secret-key", "16k_zh_en_2.0", 200,
            nonce=42, timestamp=1700000000)
        self.assertTrue(url.startswith("wss://asr.cloud.tencent.com/asr/v2/12345?"))
        self.assertEqual(params["voice_format"], 1)
        self.assertEqual(params["needvad"], 1)
        self.assertEqual(params["sentence_strategy"], 0)
        self.assertEqual(params["nonce"], 42)
        self.assertEqual(params["expired"], 1700000000 + 3600)
        # 用返回的 params 独立复算签名，必须与 URL 中一致
        canonical = probe.build_canonical(params, "12345")
        expect_sig = probe.sign_canonical(canonical, "secret-key")
        got_sig = parse_qs(urlparse(url).query)["signature"][0]
        self.assertEqual(got_sig, expect_sig)
        self.assertNotIn("secret-key", url)       # 密钥绝不进 URL

    def test_voice_id_unique_per_connection(self):
        ids = {probe.build_ws_url("1", "s", "k", "e", 200)[1]["voice_id"] for _ in range(5)}
        self.assertEqual(len(ids), 5)

    def test_redact_secret_id(self):
        self.assertEqual(probe.redact_secret_id("AKIDabcdefg12345"), "AKID****45")


# ---------------------------------------------------------------------------
# 结果解析与归并
# ---------------------------------------------------------------------------
class TestParsingAndTracker(unittest.TestCase):
    def test_extract_sentence_items_variants(self):
        item = {"sentence_id": 3, "sentence_type": 1, "speaker_id": 0,
                "sentence": "你好", "start_time": 1, "end_time": 2}
        # 官方 SDK 形态：sentences 为 dict，含 sentence_list
        self.assertEqual(probe.extract_sentence_items(
            {"sentences": {"sentence_list": [item]}})[0]["text"], "你好")
        # 兼容 list 形态与 text 字段
        self.assertEqual(probe.extract_sentence_items(
            {"sentences": [{"sentence_id": 1, "text": "hi"}]})[0]["text"], "hi")
        self.assertEqual(probe.extract_sentence_items({"code": 0}), [])

    def test_tracker_interim_then_final(self):
        t = probe.TranscriptTracker()
        self.assertEqual(t.update({"sentence_id": 0, "sentence_type": 0, "text": "今"}), "interim")
        self.assertEqual(t.update({"sentence_id": 0, "sentence_type": 0, "text": "今天"}), "interim")
        self.assertEqual(t.update({"sentence_id": 0, "sentence_type": 1, "text": "今天"}), "final")
        self.assertEqual(t.snapshot()[0]["text"], "今天")
        self.assertTrue(t.snapshot()[0]["is_final"])

    def test_tracker_final_is_frozen_against_late_messages(self):
        t = probe.TranscriptTracker()
        t.update({"sentence_id": 0, "sentence_type": 1, "text": "定稿内容"})
        # 旧连接迟到的重复/修订消息不得改动已定稿句子
        self.assertEqual(t.update({"sentence_id": 0, "sentence_type": 0, "text": "别的"}), "duplicate")
        self.assertEqual(t.snapshot()[0]["text"], "定稿内容")

    def test_tracker_ignores_empty_dup_shorter_out_of_order(self):
        t = probe.TranscriptTracker()
        self.assertEqual(t.update({"sentence_id": 0, "sentence_type": 0, "text": "  "}), "ignored")
        self.assertEqual(t.update({"sentence_id": -1, "sentence_type": 1, "text": "x"}), "ignored")
        t.update({"sentence_id": 1, "sentence_type": 0, "text": "长文本内容"})
        self.assertEqual(t.update({"sentence_id": 1, "sentence_type": 0, "text": "长"}), "ignored")
        t.update({"sentence_id": 2, "sentence_type": 1, "text": "第二句"})
        self.assertEqual(t.update({"sentence_id": 2, "sentence_type": 1, "text": "第二句"}), "duplicate")
        self.assertEqual(len(t.snapshot()), 2)


# ---------------------------------------------------------------------------
# 音频处理
# ---------------------------------------------------------------------------
class TestAudio(unittest.TestCase):
    def test_resample_length_ratio(self):
        n = 4800  # 0.1s @48k
        data = struct.pack(f"<{n}h", *([0, 1000, -1000] * (n // 3)))
        out = probe.resample_pcm16(data, 48000, 16000)
        self.assertAlmostEqual(len(out) / 2, n / 3, delta=2)

    def test_resample_noop_and_empty(self):
        self.assertEqual(probe.resample_pcm16(b"", 48000, 16000), b"")
        self.assertEqual(probe.resample_pcm16(b"\x01\x00", 16000, 16000), b"\x01\x00")

    def test_linear_resampler_chunked_equals_whole(self):
        # 跨块相位连续：一次喂入与分两块喂入结果应完全一致
        n = 3000
        samples = [int(10000 * ((i % 97) / 97 - 0.5)) for i in range(n)]
        data = struct.pack(f"<{n}h", *samples)
        whole = probe.LinearResampler(48000, 16000).process(data)
        r = probe.LinearResampler(48000, 16000)
        chunked = r.process(data[: 2000]) + r.process(data[2000:])
        self.assertEqual(whole, chunked)

    def _write_wav(self, path, rate, channels, seconds):
        n = int(rate * seconds)
        frames = b"".join(
            struct.pack(f"<{channels}h", *([1000] * channels)) for _ in range(n))
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(channels)
            wf.setsampwidth(2)
            wf.setframerate(rate)
            wf.writeframes(frames)

    def test_read_wav_stereo_48k_to_16k_mono(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td) / "t.wav"
            self._write_wav(p, 48000, 2, 0.5)
            pcm, dur = probe.read_wav_as_pcm16k(str(p))
            self.assertAlmostEqual(dur, 0.5, delta=0.01)
            self.assertAlmostEqual(len(pcm), 16000, delta=64)   # 0.5s @16k*2B
            # 立体声取均值后仍为 1000
            self.assertEqual(struct.unpack("<1h", pcm[:2])[0], 1000)

    def test_split_frames_200ms(self):
        pcm = b"\x00" * 16000                       # 0.5s @16k 16bit
        frames = probe.split_frames(pcm, 6400)      # 200ms/包
        self.assertEqual([len(f) for f in frames], [6400, 6400, 3200])


# ---------------------------------------------------------------------------
# WebSocket 客户端（本地回环服务端）
# ---------------------------------------------------------------------------
def _srv_send_frame(conn, opcode, payload):
    header = bytes([0x80 | opcode])          # 服务端帧不掩码
    n = len(payload)
    if n < 126:
        header += bytes([n])
    elif n < 65536:
        header += bytes([126]) + struct.pack(">H", n)
    else:
        header += bytes([127]) + struct.pack(">Q", n)
    conn.sendall(header + payload)


def _srv_recv_frame(conn):
    b0 = conn.recv(2)
    if len(b0) < 2:
        return None
    opcode = b0[0] & 0x0F
    masked = bool(b0[1] & 0x80)
    length = b0[1] & 0x7F
    if length == 126:
        length = struct.unpack(">H", conn.recv(2))[0]
    elif length == 127:
        length = struct.unpack(">Q", conn.recv(8))[0]
    mask = conn.recv(4) if masked else b""
    payload = b""
    while len(payload) < length:
        chunk = conn.recv(length - len(payload))
        if not chunk:
            break
        payload += chunk
    if masked:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return opcode, masked, payload


class TestMiniWebSocket(unittest.TestCase):
    def _run_server(self, ready: threading.Event, results: dict):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        results["port"] = srv.getsockname()[1]
        srv.listen(1)
        ready.set()
        conn, _ = srv.accept()
        # --- 握手 ---
        data = b""
        while b"\r\n\r\n" not in data:
            data += conn.recv(4096)
        head = data.split(b"\r\n\r\n")[0].decode("utf-8", "replace")
        key = re.search(r"Sec-WebSocket-Key: (\S+)", head).group(1)
        results["request_path"] = head.split(" ")[1]
        accept = base64.b64encode(
            hashlib.sha1((key + probe.WS_GUID).encode()).digest()).decode()
        conn.sendall((
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
        # --- ping，期待客户端自动 pong ---
        _srv_send_frame(conn, 0x9, b"hi")
        opcode, masked, payload = _srv_recv_frame(conn)
        results["pong"] = (opcode, payload)
        # --- 文本消息 ---
        msg = {"code": 0, "message": "success", "voice_id": "v1",
               "sentences": {"sentence_list": [
                   {"sentence_id": 0, "sentence_type": 1, "speaker_id": -1,
                    "sentence": "回环测试", "start_time": 0, "end_time": 100}]}}
        _srv_send_frame(conn, 0x1, json.dumps(msg, ensure_ascii=False).encode("utf-8"))
        # --- 客户端发来的二进制（规范要求掩码） ---
        opcode, masked, payload = _srv_recv_frame(conn)
        results["binary"] = (opcode, masked, payload)
        # --- 关闭 ---
        _srv_send_frame(conn, 0x8, b"")
        conn.close()
        srv.close()

    def test_loopback_handshake_mask_ping_close(self):
        ready = threading.Event()
        results = {}
        threading.Thread(target=self._run_server, args=(ready, results), daemon=True).start()
        ready.wait(5)
        ws = probe.MiniWebSocket(f"ws://127.0.0.1:{results['port']}/asr/v2/1?a=1", timeout=5.0)
        self.assertEqual(results["request_path"], "/asr/v2/1?a=1")
        # ping 应被自动应答 pong
        frame = ws.recv(timeout=5.0)
        self.assertIsNotNone(frame)
        opcode, payload = frame
        self.assertEqual(opcode, 0x1)
        msg = json.loads(payload.decode("utf-8"))
        self.assertEqual(probe.extract_sentence_items(msg)[0]["text"], "回环测试")
        self.assertEqual(results["pong"][0], 0xA)          # opcode pong
        # 客户端发送二进制：服务端应看到掩码帧且负载一致
        ws.send_binary(b"\x01\x02\x03")
        import time
        for _ in range(50):
            if "binary" in results:
                break
            time.sleep(0.05)
        opcode, masked, payload = results["binary"]
        self.assertEqual(opcode, 0x2)
        self.assertTrue(masked)                             # RFC：客户端帧必须掩码
        self.assertEqual(payload, b"\x01\x02\x03")
        # 服务端 close → recv 抛连接结束
        with self.assertRaises(ConnectionError):
            for _ in range(10):
                ws.recv(timeout=1.0)
        ws.close()


# ---------------------------------------------------------------------------
# 错误码提示
# ---------------------------------------------------------------------------
class TestErrorHints(unittest.TestCase):
    def test_doc_codes_covered(self):
        for code in (4000, 4001, 4002, 4003, 4004, 4005, 4006, 4007,
                     4008, 4009, 4010, 5000, 5001, 5002, 6001):
            self.assertIn(code, probe.ERROR_HINTS)


if __name__ == "__main__":
    unittest.main()
