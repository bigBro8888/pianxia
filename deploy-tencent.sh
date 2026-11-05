#!/usr/bin/env bash
# 在腾讯云 CVM（Ubuntu/Debian）上一键部署片匣
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/pianxia}"
REPO_URL="${REPO_URL:-https://github.com/bigBro8888/pianxia.git}"
PORT="${PORT:-8765}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "请用 root 或 sudo 运行：sudo bash deploy-tencent.sh"
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y ca-certificates curl git

if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh
fi

systemctl enable --now docker

mkdir -p "$APP_DIR"
if [[ -d "$APP_DIR/.git" ]]; then
  git -C "$APP_DIR" fetch --depth 1 origin main
  git -C "$APP_DIR" reset --hard origin/main
else
  rm -rf "$APP_DIR"
  git clone --depth 1 -b main "$REPO_URL" "$APP_DIR"
fi

cd "$APP_DIR"
mkdir -p downloads
docker compose down --remove-orphans || true
docker compose up -d --build

echo
echo "部署完成。"
echo "本机健康检查：curl -s http://127.0.0.1:${PORT}/api/health"
echo "公网访问前请在腾讯云安全组放行 TCP ${PORT}"
echo "访问地址：http://<公网IP>:${PORT}/"
