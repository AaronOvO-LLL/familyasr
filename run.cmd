@echo off
rem 家语字幕屏 Windows 启动脚本
rem 前置：python -m venv .venv && .venv\Scripts\python -m pip install -r requirements.txt
rem 配置：复制 .env.example 为 .env 并填入访问码（及真实链路所需的腾讯云凭据）
cd /d "%~dp0"
.\.venv\Scripts\python.exe scripts\run_server.py
