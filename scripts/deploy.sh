#!/usr/bin/env bash
# 本地一键发布：push GitHub → 服务器拉取重启 → 公网健康检查
# 用法：bash scripts/deploy.sh
set -euo pipefail
cd "$(dirname "$0")/.."

SERVER=${SERVER:-ubuntu@129.204.31.88}
SITE=${SITE:-https://asr.hao-rizon.com}

echo "== 1/3 推送 GitHub =="
git push origin main

echo "== 2/3 服务器发布 =="
ssh "$SERVER" 'bash -s' < scripts/remote-deploy.sh

echo "== 3/3 公网健康检查 =="
curl -sf "$SITE/health" && echo ""
echo "发布完成：$SITE/?token=<ACCESS_TOKEN>"
