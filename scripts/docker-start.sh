#!/usr/bin/env bash
# agentic-app 容器启动脚本
#
# 由 docker-compose.yml 的 entrypoint 调用，容器每次启动都会执行它。
# 脚本随代码目录挂载进容器，改完执行 `docker compose restart` 即生效，不需要重建镜像。
#
# 可用环境变量：
#   APP_HOST=0.0.0.0            监听地址
#   APP_PORT=8001               监听端口
#   RELOAD=true                 开启 uvicorn --reload（改代码自动重载，开发用）
#   INSTALL_DEPS=1              启动前 pip install -r requirements.txt
#   WAIT_FOR_HOST=host:port,... 启动前等待依赖服务可达，例如 host.docker.internal:36379
#   WAIT_TIMEOUT=60             每个依赖的最长等待秒数
#   EXTRA_UVICORN_ARGS=         追加的 uvicorn 参数
#   MODE=shell                  只保活容器，不启动服务（等价 docker compose exec/attach 调试）
set -euo pipefail

APP_DIR="${APP_DIR:-/root/agentic-app}"

log() { printf '[start %s] %s\n' "$(date '+%F %T')" "$*"; }
die() { printf '[start %s] 错误: %s\n' "$(date '+%F %T')" "$*" >&2; exit 1; }

APP_HOST="${APP_HOST:-0.0.0.0}"
APP_PORT="${APP_PORT:-8001}"
RELOAD="${RELOAD:-false}"
INSTALL_DEPS="${INSTALL_DEPS:-0}"
WAIT_FOR_HOST="${WAIT_FOR_HOST:-}"
WAIT_TIMEOUT="${WAIT_TIMEOUT:-60}"
EXTRA_UVICORN_ARGS="${EXTRA_UVICORN_ARGS:-}"
MODE="${MODE:-serve}"

[ -d "$APP_DIR" ] || die "代码目录不存在: $APP_DIR（检查 compose 里的 volumes 挂载）"
cd "$APP_DIR"

log "工作目录: $(pwd)"
log "python: $(python3 -V 2>&1)  uvicorn: $(python3 -m uvicorn --version 2>&1 | head -1)"

if [ "$MODE" = "shell" ]; then
  log "MODE=shell，仅保活容器"
  exec sleep infinity
fi

[ -f app.py ] || die "找不到 app.py，确认挂载的是项目根目录"

if [ "$INSTALL_DEPS" = "1" ]; then
  [ -f requirements.txt ] || die "INSTALL_DEPS=1 但找不到 requirements.txt"
  log "安装依赖: pip install -r requirements.txt"
  python3 -m pip install --no-cache-dir -r requirements.txt
fi

# 等待依赖（redis / 宿主机 MCP 等）可达，避免启动瞬间就报连接失败
wait_for() {
  local target="$1" host="${1%:*}" port="${1##*:}"
  python3 - "$host" "$port" "$WAIT_TIMEOUT" <<'PY'
import os, socket, sys, time

host, port, timeout = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
deadline = time.time() + timeout
while time.time() < deadline:
    try:
        with socket.create_connection((host, port), timeout=2):
            sys.exit(0)
    except OSError:
        time.sleep(2)
print(f"等待 {host}:{port} 超时（{timeout:.0f}s）", file=sys.stderr)
sys.exit(1)
PY
}

if [ -n "$WAIT_FOR_HOST" ]; then
  IFS=',' read -ra targets <<< "$WAIT_FOR_HOST"
  for target in "${targets[@]}"; do
    target="$(echo "$target" | tr -d '[:space:]')"
    [ -n "$target" ] || continue
    log "等待依赖 $target（最长 ${WAIT_TIMEOUT}s）"
    wait_for "$target" || die "依赖 $target 不可达"
    log "依赖 $target 已就绪"
  done
fi

UVICORN_ARGS=(app:app --host "$APP_HOST" --port "$APP_PORT")
case "$RELOAD" in
  1|true|TRUE|yes) UVICORN_ARGS+=(--reload) ;;
esac
# shellcheck disable=SC2206
[ -n "$EXTRA_UVICORN_ARGS" ] && UVICORN_ARGS+=($EXTRA_UVICORN_ARGS)

log "启动: python3 -m uvicorn ${UVICORN_ARGS[*]}"
# exec 让 uvicorn 直接接住容器的 SIGTERM，docker compose stop 能干净退出
exec python3 -m uvicorn "${UVICORN_ARGS[@]}"
