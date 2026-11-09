#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""启动家语字幕屏服务（Windows / Ubuntu 通用）。

读取 .env / 环境变量（见 .env.example），启动前做配置校验。
TLS 证书与私钥配置了就走 HTTPS/WSS（局域网 Pad 试用，见 M2 文档），
否则纯 HTTP（仅限 localhost 开发）。

用法：
  python scripts/run_server.py
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import uvicorn  # noqa: E402

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = Settings.from_env()
    problems = settings.validate()
    if problems:
        print("配置无效，无法启动：")
        for p in problems:
            print(f"  - {p}")
        print("请参照 .env.example 配置 .env（密钥勿入库、勿发聊天）。")
        return 2

    app = create_app(settings)
    ssl_kwargs: dict = {}
    if settings.tls_cert_file and settings.tls_key_file:
        ssl_kwargs = {"ssl_certfile": settings.tls_cert_file,
                      "ssl_keyfile": settings.tls_key_file}
        scheme = "https"
    else:
        scheme = "http"
        if settings.host != "127.0.0.1":
            print("[警告] 未配置 TLS 却绑定非回环地址：Android 浏览器将拒绝麦克风"
                  "（非安全上下文）。局域网试用请按 M2 文档配置 HTTPS。")

    print(f"家语字幕屏启动：{scheme}://{settings.host}:{settings.port}/"
          f"（提供方={settings.provider}，"
          f"每日上限 {settings.daily_limit_seconds / 60:.0f} 分钟，"
          f"单次上限 {settings.session_limit_seconds / 60:.0f} 分钟）")
    print("页面需带访问码：在上述地址后加 ?token=<ACCESS_TOKEN>")
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info",
                **ssl_kwargs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
