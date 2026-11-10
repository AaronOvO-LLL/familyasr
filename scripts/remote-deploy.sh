#!/usr/bin/env bash
# 服务器端发布（由本地 scripts/deploy.sh 经 ssh 管道送入执行）：
#   镜像式同步到 origin/main → 依赖同步 → 重启服务 → 本机健康检查
# 镜像式 = 服务器工作区永远等于某个提交，回滚就是 git reset --hard <sha>
set -euo pipefail

APP_DIR=/srv/familyasr
cd "$APP_DIR"

git fetch origin main
BEFORE=$(git rev-parse --short HEAD || echo none)
git reset --hard origin/main
AFTER=$(git rev-parse --short HEAD)
echo "版本：$BEFORE -> $AFTER"

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
# 国内网络用清华镜像，失败回退官方源
.venv/bin/pip install -q -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt \
  || .venv/bin/pip install -q -r requirements.txt

sudo systemctl restart familyasr
sleep 2
curl -sf http://127.0.0.1:7020/health >/dev/null
echo "本机健康检查通过：http://127.0.0.1:7020/health"
echo "DEPLOY_OK $AFTER"
