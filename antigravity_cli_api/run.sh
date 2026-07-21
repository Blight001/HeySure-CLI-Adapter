#!/usr/bin/env bash
# Pure-Python Antigravity OAuth -> OpenAI-compatible Linux gateway manager.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
  SERVICE_DIR="${ANTIGRAVITY_SERVICE_DIR:-/var/lib/heysure-antigravity-cli-api}"
else
  SERVICE_DIR="$ROOT"
fi
RUNTIME_DIR="$SERVICE_DIR/runtime"
SERVER_FILE="$SERVICE_DIR/server.py"
AUTH_FILE="${ANTIGRAVITY_AUTH_FILE:-$RUNTIME_DIR/antigravity-auth.json}"
PID_FILE="$RUNTIME_DIR/gateway.pid"
LOG_FILE="$RUNTIME_DIR/gateway.log"
ENV_FILE="$ROOT/.env"
PROXY_FILE="$ROOT/.env.proxy"
PYTHON_BIN="${PYTHON:-python3}"

log() { printf '[antigravity] %s\n' "$*"; }
die() { printf '[antigravity] ERROR: %s\n' "$*" >&2; exit 1; }

run_root() {
  if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
    "$@"
  else
    command -v sudo >/dev/null 2>&1 || die "安装系统依赖需要 root 权限或 sudo"
    sudo "$@"
  fi
}

load_env() {
  [[ -f "$ENV_FILE" ]] && source "$ENV_FILE"
  [[ -f "$PROXY_FILE" ]] && source "$PROXY_FILE"
  export ANTIGRAVITY_HOST="${ANTIGRAVITY_HOST:-127.0.0.1}"
  export ANTIGRAVITY_PORT="${ANTIGRAVITY_PORT:-8110}"
  export ANTIGRAVITY_TIMEOUT="${ANTIGRAVITY_TIMEOUT:-600}"
  export ANTIGRAVITY_MODELS="${ANTIGRAVITY_MODELS:-gemini-pro-agent,gemini-3.1-pro-low,gemini-3.5-flash-low,gemini-3.1-flash-lite}"
  export ANTIGRAVITY_RUN_USER="${ANTIGRAVITY_RUN_USER:-antigravity-api}"
  export ANTIGRAVITY_AUTH_FILE="${ANTIGRAVITY_AUTH_FILE:-$AUTH_FILE}"
  export ANTIGRAVITY_PUBLIC_BASE_URL="${ANTIGRAVITY_PUBLIC_BASE_URL:-}"
  AUTH_FILE="$ANTIGRAVITY_AUTH_FILE"
}

install_deps() {
  if command -v dnf >/dev/null 2>&1; then
    run_root dnf install -y python3 curl ca-certificates
  elif command -v yum >/dev/null 2>&1; then
    run_root yum install -y python3 curl ca-certificates
  elif command -v apt-get >/dev/null 2>&1; then
    run_root apt-get update
    run_root apt-get install -y python3 curl ca-certificates
  else
    die "不支持当前包管理器；请手动安装 Python 3.9+、curl 和 CA 证书"
  fi
}

cmd_deps() {
  load_env
  if ! command -v "$PYTHON_BIN" >/dev/null 2>&1 || ! command -v curl >/dev/null 2>&1; then
    log "正在安装 Python 3、curl 和 CA 证书……"
    install_deps
  fi
  command -v "$PYTHON_BIN" >/dev/null 2>&1 || die "未找到 Python 3"
  command -v curl >/dev/null 2>&1 || die "未找到 curl"
  "$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' \
    || die "Python 版本过低：$($PYTHON_BIN --version 2>&1)，要求 3.9+"
  log "依赖已就绪：$($PYTHON_BIN --version 2>&1)，无需 Node.js/npm"
}

save_env_value() {
  local key="$1"
  local value="$2"
  local temp_file
  temp_file="$(mktemp "${ROOT}/.env.XXXXXX")"
  if [[ -f "$ENV_FILE" ]]; then
    awk -v key="$key" '
      $0 !~ "^[[:space:]]*export[[:space:]]+" key "=" &&
      $0 !~ "^[[:space:]]*" key "=" { print }
    ' "$ENV_FILE" > "$temp_file"
  fi
  printf 'export %s=%q\n' "$key" "$value" >> "$temp_file"
  chmod 600 "$temp_file"
  mv -f "$temp_file" "$ENV_FILE"
}

ensure_gateway_api_key() {
  [[ -n "${ANTIGRAVITY_API_KEY:-}" ]] && return
  ANTIGRAVITY_API_KEY="$($PYTHON_BIN -c 'import secrets; print("ag-" + secrets.token_urlsafe(32))')"
  export ANTIGRAVITY_API_KEY
  save_env_value ANTIGRAVITY_API_KEY "$ANTIGRAVITY_API_KEY"
  log "已生成本地网关 API Key，并保存到 $ENV_FILE（权限 600）"
}

ensure_oauth_client() {
  if [[ -n "${ANTIGRAVITY_OAUTH_CLIENT_ID:-}" && -n "${ANTIGRAVITY_OAUTH_CLIENT_SECRET:-}" ]]; then
    return
  fi
  echo
  echo "首次登录需要 Google OAuth 桌面应用凭据。凭据只保存到已忽略的 .env，不会提交 Git。"
  echo "请在 Google Cloud Console 创建 OAuth Client（应用类型：Desktop app），并把当前账号加入测试用户。"
  local client_id=""
  local client_secret=""
  printf 'OAuth Client ID > '
  read -r client_id || return 1
  printf 'OAuth Client Secret（输入时不显示）> '
  read -rs client_secret || return 1
  printf '\n'
  [[ -n "$client_id" && -n "$client_secret" ]] || die "OAuth Client ID 和 Client Secret 均不能为空"
  ANTIGRAVITY_OAUTH_CLIENT_ID="$client_id"
  ANTIGRAVITY_OAUTH_CLIENT_SECRET="$client_secret"
  export ANTIGRAVITY_OAUTH_CLIENT_ID ANTIGRAVITY_OAUTH_CLIENT_SECRET
  save_env_value ANTIGRAVITY_OAUTH_CLIENT_ID "$ANTIGRAVITY_OAUTH_CLIENT_ID"
  save_env_value ANTIGRAVITY_OAUTH_CLIENT_SECRET "$ANTIGRAVITY_OAUTH_CLIENT_SECRET"
  log "OAuth client 已保存到 $ENV_FILE（权限 600）"
}

print_heysure_config() {
  local model="${ANTIGRAVITY_MODELS%%,*}"
  local public_url="$ANTIGRAVITY_PUBLIC_BASE_URL"
  if [[ -z "$public_url" ]]; then
    local display_host="$ANTIGRAVITY_HOST"
    [[ "$display_host" == "0.0.0.0" || "$display_host" == "::" ]] && display_host="127.0.0.1"
    public_url="http://${display_host}:${ANTIGRAVITY_PORT}/v1/chat/completions"
  fi
  echo
  echo "========== HeySure 模型配置 =========="
  echo "显示名称：Antigravity Gemini"
  echo "模型名：  $model"
  echo "Base URL：$public_url"
  echo "API Key： $ANTIGRAVITY_API_KEY"
  echo "接口协议：OpenAI 兼容"
  echo "工具协议：原生 Function Calling"
  echo "其他模型：$ANTIGRAVITY_MODELS"
  echo "======================================="
}

cmd_config() {
  load_env
  command -v "$PYTHON_BIN" >/dev/null 2>&1 || cmd_deps
  ensure_gateway_api_key
  print_heysure_config
}

ensure_service_user() {
  if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    RUN_USER_NAME="$(id -un)"
    RUN_USER_HOME="$HOME"
    return
  fi
  RUN_USER_NAME="$ANTIGRAVITY_RUN_USER"
  [[ "$RUN_USER_NAME" =~ ^[a-z_][a-z0-9_-]*[$]?$ ]] \
    || die "ANTIGRAVITY_RUN_USER 不是合法的 Linux 用户名：$RUN_USER_NAME"
  [[ "$RUN_USER_NAME" != "root" ]] || die "请使用普通运行用户，不要把 ANTIGRAVITY_RUN_USER 设为 root"
  if ! id "$RUN_USER_NAME" >/dev/null 2>&1; then
    command -v useradd >/dev/null 2>&1 || die "系统缺少 useradd"
    log "正在创建专用普通用户：$RUN_USER_NAME"
    useradd --create-home --shell /bin/bash "$RUN_USER_NAME"
  fi
  RUN_USER_HOME="$(getent passwd "$RUN_USER_NAME" | cut -d: -f6)"
  [[ -n "$RUN_USER_HOME" && -d "$RUN_USER_HOME" ]] || die "无法确定用户 $RUN_USER_NAME 的 home 目录"
}

prepare_runtime() {
  ensure_service_user
  if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
    command -v runuser >/dev/null 2>&1 || die "系统缺少 runuser（请安装 util-linux）"
    mkdir -p "$SERVICE_DIR" "$RUNTIME_DIR"
    install -m 0644 "$ROOT/server.py" "$SERVER_FILE"
    chown -R "$RUN_USER_NAME":"$RUN_USER_NAME" "$SERVICE_DIR"
  else
    mkdir -p "$RUNTIME_DIR"
  fi
}

run_as_service_user() {
  local -a forwarded
  forwarded=(
    "ANTIGRAVITY_HOST=$ANTIGRAVITY_HOST"
    "ANTIGRAVITY_PORT=$ANTIGRAVITY_PORT"
    "ANTIGRAVITY_TIMEOUT=$ANTIGRAVITY_TIMEOUT"
    "ANTIGRAVITY_MODELS=$ANTIGRAVITY_MODELS"
    "ANTIGRAVITY_AUTH_FILE=$AUTH_FILE"
    "ANTIGRAVITY_API_KEY=${ANTIGRAVITY_API_KEY:-}"
    "ANTIGRAVITY_OAUTH_CALLBACK_PORT=${ANTIGRAVITY_OAUTH_CALLBACK_PORT:-51121}"
    "HTTP_PROXY=${HTTP_PROXY:-}" "HTTPS_PROXY=${HTTPS_PROXY:-}" "ALL_PROXY=${ALL_PROXY:-}"
    "http_proxy=${http_proxy:-}" "https_proxy=${https_proxy:-}" "all_proxy=${all_proxy:-}"
    "NO_PROXY=${NO_PROXY:-localhost,127.0.0.1,::1}"
    "no_proxy=${no_proxy:-localhost,127.0.0.1,::1}"
  )
  local name
  for name in ANTIGRAVITY_OAUTH_CLIENT_ID ANTIGRAVITY_OAUTH_CLIENT_SECRET \
    ANTIGRAVITY_BASE_URLS ANTIGRAVITY_USER_AGENT ANTIGRAVITY_VERSION_MANIFEST \
    ANTIGRAVITY_TOKEN_ENDPOINT ANTIGRAVITY_AUTH_ENDPOINT ANTIGRAVITY_USERINFO_ENDPOINT; do
    [[ -n "${!name:-}" ]] && forwarded+=("$name=${!name}")
  done
  if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    env "${forwarded[@]}" "$@"
  else
    runuser -u "$RUN_USER_NAME" -- env \
      HOME="$RUN_USER_HOME" USER="$RUN_USER_NAME" LOGNAME="$RUN_USER_NAME" PATH="$PATH" \
      "${forwarded[@]}" "$@"
  fi
}

pid_of() {
  [[ -f "$PID_FILE" ]] || return 1
  local pid
  pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null && printf '%s' "$pid"
}

cmd_login() {
  load_env
  command -v "$PYTHON_BIN" >/dev/null 2>&1 || cmd_deps
  ensure_oauth_client
  prepare_runtime
  log "将使用普通用户 $RUN_USER_NAME 保存 Antigravity OAuth 凭证"
  log "远程服务器可在本机建立 SSH 隧道：ssh -L 51121:127.0.0.1:51121 <user>@服务器"
  run_as_service_user "$PYTHON_BIN" "$SERVER_FILE" login \
    --auth-file "$AUTH_FILE" --callback-port "${ANTIGRAVITY_OAUTH_CALLBACK_PORT:-51121}" --no-browser
  ensure_gateway_api_key
  print_heysure_config
}

cmd_auth_status() {
  load_env
  prepare_runtime
  run_as_service_user "$PYTHON_BIN" "$SERVER_FILE" auth-status --auth-file "$AUTH_FILE"
}

require_auth() {
  [[ -f "$AUTH_FILE" ]] || die "尚未登录；请先运行 $0 login"
}

cmd_proxy() {
  local url="${1:-}"
  if [[ "$url" == "clear" ]]; then
    rm -f "$PROXY_FILE"
    log "已清除代理配置"
    return
  fi
  [[ -n "$url" ]] || die "用法：$0 proxy http://host:port；清除：$0 proxy clear"
  umask 077
  {
    printf 'export http_proxy=%q\n' "$url"
    printf 'export https_proxy=%q\n' "$url"
    printf 'export HTTP_PROXY=%q\n' "$url"
    printf 'export HTTPS_PROXY=%q\n' "$url"
    printf 'export ALL_PROXY=%q\n' "$url"
    printf 'export NO_PROXY=%q\n' "localhost,127.0.0.1,::1"
  } > "$PROXY_FILE"
  log "代理已保存到 $PROXY_FILE"
}

cmd_start() {
  load_env
  command -v "$PYTHON_BIN" >/dev/null 2>&1 || cmd_deps
  ensure_gateway_api_key
  prepare_runtime
  require_auth
  if pid="$(pid_of 2>/dev/null)"; then
    log "网关已运行（pid $pid）"
    return
  fi
  local gateway_pid
  gateway_pid="$(run_as_service_user bash -c '
    nohup "$1" "$2" serve --auth-file "$3" >> "$4" 2>&1 </dev/null &
    echo $!
  ' bash "$PYTHON_BIN" "$SERVER_FILE" "$AUTH_FILE" "$LOG_FILE")"
  [[ "$gateway_pid" =~ ^[0-9]+$ ]] || die "未取得网关进程 PID：$gateway_pid"
  echo "$gateway_pid" > "$PID_FILE"
  sleep 1
  pid_of >/dev/null || { tail -n 30 "$LOG_FILE" >&2 || true; die "网关启动失败"; }
  log "已启动：http://${ANTIGRAVITY_HOST}:${ANTIGRAVITY_PORT}/v1/chat/completions"
  log "运行用户：$RUN_USER_NAME；日志：$LOG_FILE"
}

cmd_stop() {
  if ! pid="$(pid_of 2>/dev/null)"; then
    rm -f "$PID_FILE"
    log "网关未运行"
    return
  fi
  kill "$pid" 2>/dev/null || true
  for _ in {1..20}; do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.25
  done
  kill -9 "$pid" 2>/dev/null || true
  rm -f "$PID_FILE"
  log "已停止"
}

cmd_status() {
  load_env
  if pid="$(pid_of 2>/dev/null)"; then
    local owner
    owner="$(ps -o user= -p "$pid" 2>/dev/null | xargs || true)"
    log "运行中（pid $pid，用户 ${owner:-unknown}）http://${ANTIGRAVITY_HOST}:${ANTIGRAVITY_PORT}/"
    curl -fsS "http://${ANTIGRAVITY_HOST}:${ANTIGRAVITY_PORT}/health" 2>/dev/null || true
    printf '\n'
  else
    log "未运行"
    return 1
  fi
}

ensure_log_file() {
  load_env
  prepare_runtime
  run_as_service_user touch "$LOG_FILE"
}

cmd_logs() { ensure_log_file; tail -n 100 "$LOG_FILE"; }

cmd_fg() {
  load_env
  command -v "$PYTHON_BIN" >/dev/null 2>&1 || cmd_deps
  ensure_gateway_api_key
  prepare_runtime
  require_auth
  log "以前台模式启动，运行用户：$RUN_USER_NAME"
  run_as_service_user "$PYTHON_BIN" "$SERVER_FILE" serve --auth-file "$AUTH_FILE"
}

cmd_proxy_interactive() {
  local url
  echo "请输入代理 URL（例如 http://127.0.0.1:7890）；输入 clear 清除，直接回车取消。"
  printf '代理 URL > '
  read -r url || return 0
  [[ -n "$url" ]] && cmd_proxy "$url"
}

menu() {
  while true; do
    echo
    echo "========== Antigravity Python 网关 =========="
    echo "  1) 检查 / 自动安装 Python 依赖"
    echo "  2) Google OAuth 登录 Antigravity"
    echo "  3) 查看登录状态"
    echo "  4) 显示 HeySure 模型配置"
    echo "  5) 配置代理"
    echo "  6) 启动网关"
    echo "  7) 停止网关"
    echo "  8) 重启网关"
    echo "  9) 查看服务状态"
    echo "  l) 查看最近日志"
    echo "  a) 前台启动调试"
    echo "  0) 退出"
    echo "=============================================="
    printf '请选择 > '
    local choice
    read -r choice || return 0
    case "$choice" in
      1) cmd_deps ;;
      2) cmd_login || true ;;
      3) cmd_auth_status || true ;;
      4) cmd_config ;;
      5) cmd_proxy_interactive ;;
      6) cmd_start ;;
      7) cmd_stop ;;
      8) cmd_stop; cmd_start ;;
      9) cmd_status || true ;;
      l|L) cmd_logs ;;
      a|A) cmd_fg ;;
      0|q|Q) return 0 ;;
      *) log "无效选项：$choice" ;;
    esac
  done
}

usage() {
  cat <<'EOF'
用法：./run.sh [command]
  deps                 检查并自动安装 Python 3 / curl（不需要 Node.js）
  login                Google OAuth 登录 Antigravity
  auth-status          查看账号、项目与令牌到期时间
  config               生成/显示 HeySure 要填写的模型、URL 和本地 API Key
  proxy <url>|clear    配置或清除代理
  start|stop|restart   后台启停 Python 网关
  status               查看状态和健康检查
  logs                 最近 100 行日志
  logs -f              持续查看日志
  fg                   前台运行

无参数直接运行时进入交互菜单。root 仅负责部署和管理，登录及网关默认以
普通用户 antigravity-api 运行。可用 ANTIGRAVITY_RUN_USER 指定已有普通用户。
EOF
}

case "${1:-}" in
  "") menu ;;
  deps) cmd_deps ;;
  login) cmd_login ;;
  auth-status) cmd_auth_status ;;
  config) cmd_config ;;
  proxy) shift; cmd_proxy "$@" ;;
  start) cmd_start ;;
  stop) cmd_stop ;;
  restart) cmd_stop; cmd_start ;;
  status) cmd_status ;;
  logs) shift; if [[ "${1:-}" == "-f" ]]; then ensure_log_file; tail -f "$LOG_FILE"; else cmd_logs; fi ;;
  fg) cmd_fg ;;
  help|-h|--help) usage ;;
  *) log "未知命令：$1"; usage; exit 1 ;;
esac
