# -*- coding: utf-8 -*-
"""应用配置：从环境变量与 .env 读取，集中校验。

安全约束（需求 FR-06）：
  * 密钥只存在于后端进程环境，绝不下发浏览器；
  * ACCESS_TOKEN 必填——没有访问码不对外提供页面与 WebSocket；
  * 开发默认 HOST=127.0.0.1；绑定 0.0.0.0 属于局域网试用模式（M2），见文档。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_env_file(path: Path) -> None:
    """把 KEY=VALUE 行并入环境变量（不覆盖已有环境变量，不回显值）。"""
    if not path.is_file():
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


@dataclass(frozen=True)
class Settings:
    # 识别提供方：mock | tencent
    provider: str = "mock"
    # 腾讯云凭据（仅后端使用）
    appid: str = ""
    secret_id: str = ""
    secret_key: str = ""
    engine_model_type: str = "16k_zh_en_2.0"
    # 说话人标签显示（V0.2）：off=仅隐藏标签（模型不变）；切回无标签模型用
    # ASR_ENGINE_MODEL_TYPE=16k_zh_en_2.0（两者效果不同，见 V0.2-说话人能力核验）
    speaker_labels: bool = True
    # 家庭访问码
    access_token: str = ""
    # 监听
    host: str = "127.0.0.1"
    port: int = 7020
    # 局域网 HTTPS（可选）
    tls_cert_file: str = ""
    tls_key_file: str = ""
    # 用量限制（秒）
    daily_limit_seconds: float = 1800.0
    session_limit_seconds: float = 7200.0
    # 生命周期
    tail_timeout_seconds: float = 3.0       # FR-01：停止后最多 3 秒收尾句
    client_idle_timeout_seconds: float = 30.0  # 心跳/空闲清理
    usage_file: Path = ROOT / "var" / "usage.json"
    # 音频约定：16kHz / 16bit / 单声道 / 200ms 一包（6400 字节）
    sample_rate: int = 16000
    packet_ms: int = 200

    @classmethod
    def from_env(cls, env_file: Path | None = None) -> "Settings":
        load_env_file(env_file or (ROOT / ".env"))
        def _f(name: str, default: float) -> float:
            raw = os.environ.get(name, "").strip()
            try:
                return float(raw) if raw else default
            except ValueError:
                return default
        return cls(
            provider=os.environ.get("ASR_PROVIDER", "mock").strip().lower() or "mock",
            appid=os.environ.get("ASR_APPID", "").strip(),
            secret_id=os.environ.get("ASR_SECRET_ID", "").strip(),
            secret_key=os.environ.get("ASR_SECRET_KEY", "").strip(),
            engine_model_type=os.environ.get(
                "ASR_ENGINE_MODEL_TYPE", "16k_zh_en_2.0").strip() or "16k_zh_en_2.0",
            speaker_labels=os.environ.get("SPEAKER_LABELS", "on").strip().lower()
            not in ("0", "off", "false", "no"),
            access_token=os.environ.get("ACCESS_TOKEN", "").strip(),
            host=os.environ.get("HOST", "127.0.0.1").strip() or "127.0.0.1",
            port=int(os.environ.get("PORT", "7020") or 7020),
            tls_cert_file=os.environ.get("TLS_CERT_FILE", "").strip(),
            tls_key_file=os.environ.get("TLS_KEY_FILE", "").strip(),
            daily_limit_seconds=_f("DAILY_LIMIT_SECONDS", 1800.0),
            session_limit_seconds=_f("SESSION_LIMIT_SECONDS", 7200.0),
        )

    def validate(self) -> list[str]:
        """启动前校验；返回致命问题列表（空 = 通过）。"""
        problems: list[str] = []
        if self.provider not in ("mock", "tencent"):
            problems.append(f"ASR_PROVIDER 只支持 mock|tencent，当前为 {self.provider!r}")
        if self.provider == "tencent":
            for name, val in (("ASR_APPID", self.appid),
                              ("ASR_SECRET_ID", self.secret_id),
                              ("ASR_SECRET_KEY", self.secret_key)):
                if not val:
                    problems.append(f"真实链路缺少 {name}（配置于 .env，勿入库）")
        if len(self.access_token) < 8:
            problems.append("ACCESS_TOKEN 必填且至少 8 位（家庭访问码）")
        if self.daily_limit_seconds <= 0 or self.session_limit_seconds <= 0:
            problems.append("用量限制必须为正数")
        if bool(self.tls_cert_file) != bool(self.tls_key_file):
            problems.append("TLS_CERT_FILE 与 TLS_KEY_FILE 必须同时提供")
        return problems

    @property
    def bytes_per_packet(self) -> int:
        """200ms @16kHz/16bit/单声道 = 6400 字节（需求 1.2 默认值）。"""
        return self.sample_rate * 2 * self.packet_ms // 1000
