#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="$(command -v python3 || true)"
if [[ -z "$PYTHON" ]]; then
  echo "[错误] 未检测到 python3。请先安装 Python 3.10+。" >&2
  exit 1
fi

if [[ ! -x ".venv/bin/python" ]]; then
  echo "[1/4] 创建 Python 虚拟环境..."
  "$PYTHON" -m venv .venv
fi

echo "[2/4] 安装或检查后端依赖..."
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r backend/requirements.txt

echo "[3/4] 启动 FastAPI 后端..."
.venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 &
SERVER_PID=$!
cleanup(){ kill "$SERVER_PID" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

echo "[4/4] 等待服务就绪并打开浏览器..."
READY=0
for _ in $(seq 1 40); do
  if curl --silent --fail http://127.0.0.1:8000/api/health >/dev/null; then READY=1; break; fi
  sleep 0.5
done
if [[ "$READY" -ne 1 ]]; then
  echo "[启动失败] 服务未能在 20 秒内就绪。" >&2
  exit 1
fi
URL="http://127.0.0.1:8000/app"
if [[ "$(uname -s)" == "Darwin" ]]; then open "$URL"
else xdg-open "$URL" >/dev/null 2>&1 || true
fi

echo "简账已启动，按 Ctrl+C 停止服务。"
wait "$SERVER_PID"
