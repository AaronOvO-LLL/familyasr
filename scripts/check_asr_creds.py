#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""凭据自检：验证 .env 里的腾讯云三元组是否可用（真实发起一次握手）。

只打印：格式元数据、握手 code/message。绝不打印任何密钥值、签名或 URL。
用法：python scripts/check_asr_creds.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import websockets

from app.asr.tencent import build_ws_url
from app.config import Settings


async def main() -> int:
    s = Settings.from_env()
    problems = s.validate()
    print("── 格式检查 ──")
    print(f"ASR_APPID:     长度 {len(s.appid)}，纯数字：{s.appid.isdigit()}"
          + ("" if s.appid.isdigit() else "  ← 应为纯数字（主账号 AppID）"))
    print(f"ASR_SECRET_ID: 长度 {len(s.secret_id)}，AKID 开头：{s.secret_id.startswith('AKID')}")
    print(f"ASR_SECRET_KEY:长度 {len(s.secret_key)}")
    if problems:
        print("配置问题：", "；".join(problems))
        return 2

    print("── 真实握手（一次连接，不发音频）──")
    url, _ = build_ws_url(s)          # URL 含签名，不打印
    try:
        async with websockets.connect(url, open_timeout=10) as ws:
            m = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
    except Exception as e:  # noqa: BLE001
        print(f"连接层异常：{type(e).__name__}: {str(e)[:200]}")
        return 3
    print(f"code: {m.get('code')}")
    print(f"message: {m.get('message')}")
    if m.get("code") == 0:
        print("✅ 鉴权通过，真实链路可用")
        return 0
    print("❌ 鉴权未通过。提示：")
    print("  * ASR_APPID 必须是【主账号】的 AppID（控制台头像→账号信息→APPID），")
    print("    不是子账号 ID/UIN；子账号 ID 填这里必然 4002。")
    print("  * SecretId/SecretKey 可以是子账号的，但必须与 AppID 属同一主账号，")
    print("    且子账号要有语音识别（ASR）的 CAM 权限。")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
