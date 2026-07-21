#!/usr/bin/env bash
# Official Antigravity CLI (agy) -> OpenAI-compatible Linux gateway manager.
#
# Windows 检出/复制后若带 CRLF，./run.sh 会报：
#   /usr/bin/env: 'bash\r': No such file or directory
# 修复（任选，会自动剥 \r）：
#   bash run.sh
#   python3 server.py fix-crlf && ./run.sh
#   sed -i 's/\r$//' run.sh && ./run.sh

# CRLF auto-heal: single-line body so it still runs when this file itself is CRLF.
# ./run.sh fails at the kernel shebang; use: bash run.sh
[ -n "${_AG_CRLF_HEALED:-}" ] || ! grep -q $'\r' "$0" 2>/dev/null || { _ag_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 1; if command -v python3 >/dev/null 2>&1; then python3 - "$_ag_dir" <<'PY'
import os, sys
root = sys.argv[1]
for name in os.listdir(root):
    if not name.endswith((".sh", ".py", ".md")):
        continue
    path = os.path.join(root, name)
    if not os.path.isfile(path):
        continue
    with open(path, "rb") as fh:
        data = fh.read()
    if b"\r" not in data:
        continue
    with open(path, "wb") as fh:
        fh.write(data.replace(b"\r\n", b"\n").replace(b"\r", b"\n"))
    if name.endswith(".sh"):
        try:
            os.chmod(path, 0o755)
        except OSError:
            pass
    print(f"[antigravity] 已去除 Windows CRLF：{path}", file=sys.stderr)
PY
elif command -v python >/dev/null 2>&1; then python - "$_ag_dir" <<'PY'
import os, sys
root = sys.argv[1]
for name in os.listdir(root):
    if not name.endswith((".sh", ".py", ".md")):
        continue
    path = os.path.join(root, name)
    if not os.path.isfile(path):
        continue
    with open(path, "rb") as fh:
        data = fh.read()
    if b"\r" not in data:
        continue
    with open(path, "wb") as fh:
        fh.write(data.replace(b"\r\n", b"\n").replace(b"\r", b"\n"))
    print(f"[antigravity] 已去除 Windows CRLF：{path}", file=sys.stderr)
PY
else tr -d '\r' <"$0" >"$_ag_dir/.run.sh.$$.lf" && mv -f "$_ag_dir/.run.sh.$$.lf" "$_ag_dir/run.sh" && chmod +x "$_ag_dir/run.sh" 2>/dev/null || true; fi; export _AG_CRLF_HEALED=1; exec /usr/bin/env bash "$_ag_dir/run.sh" "$@"; }

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
  export ANTIGRAVITY_BACKEND="${ANTIGRAVITY_BACKEND:-cli}"
  export ANTIGRAVITY_CLI_COMMAND="${ANTIGRAVITY_CLI_COMMAND:-agy}"
  export ANTIGRAVITY_MODELS="${ANTIGRAVITY_MODELS:-Gemini 3.1 Pro (Low),Gemini 3.5 Flash (Low),Gemini 3.5 Flash (High)}"
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

gen_api_key() {
  local py=""
  for py in "${PYTHON:-}" "$PYTHON_BIN" python3 python; do
    [[ -n "$py" ]] || continue
    if command -v "$py" >/dev/null 2>&1; then
      if out="$("$py" -c 'import secrets; print("ag-" + secrets.token_urlsafe(32), end="")' 2>/dev/null)" \
        && [[ -n "$out" ]]; then
        printf '%s' "$out"
        return 0
      fi
    fi
  done
  if command -v openssl >/dev/null 2>&1; then
    printf 'ag-%s' "$(openssl rand -hex 24)"
    return 0
  fi
  # 最后兜底：无 Python/openssl 时用 /dev/urandom
  if [[ -r /dev/urandom ]]; then
    printf 'ag-%s' "$(od -An -N24 -tx1 /dev/urandom 2>/dev/null | tr -d ' \n')"
    return 0
  fi
  die "无法生成 API Key：请安装 Python 3 或 openssl"
}

ensure_gateway_api_key() {
  [[ -n "${ANTIGRAVITY_API_KEY:-}" ]] && return
  ANTIGRAVITY_API_KEY="$(gen_api_key)"
  export ANTIGRAVITY_API_KEY
  save_env_value ANTIGRAVITY_API_KEY "$ANTIGRAVITY_API_KEY"
  log "已生成本地网关 API Key，并保存到 $ENV_FILE（权限 600）"
}

# ---------------------------------------------------------------------------
# expose — 对外开放 / 收回本机监听（写入 .env，start 时自动加载）
# ---------------------------------------------------------------------------

# 健康检查 / 日志展示用的本机可达地址（0.0.0.0 不能直接 curl）
local_probe_host() {
  local host="${ANTIGRAVITY_HOST:-127.0.0.1}"
  if [[ "$host" == "0.0.0.0" || "$host" == "::" ]]; then
    printf '127.0.0.1'
  else
    printf '%s' "$host"
  fi
}

expose_show() {
  load_env
  local host="${ANTIGRAVITY_HOST:-127.0.0.1}"
  echo "---- 对外开放状态 ----"
  echo "监听地址 : $host"
  echo "端口     : ${ANTIGRAVITY_PORT:-8110}"
  if [[ "$host" == "127.0.0.1" || "$host" == "localhost" || "$host" == "::1" ]]; then
    echo "范围     : 仅本机（外部/容器不可访问）"
  else
    echo "范围     : 对外开放（0.0.0.0 = 本机所有网卡，含 Docker 网桥/公网网卡）"
  fi
  if [[ -n "${ANTIGRAVITY_API_KEY:-}" ]]; then
    echo "网关密钥 : 已设置（调用需 Authorization: Bearer <key>）"
  else
    echo "网关密钥 : 未设置（任何能连上端口的人都可白嫖你的额度！）"
  fi
  if [[ -n "${ANTIGRAVITY_PUBLIC_BASE_URL:-}" ]]; then
    echo "公网 URL : $ANTIGRAVITY_PUBLIC_BASE_URL"
  fi
  echo "----------------------"
}

# 若网关在运行则重启使配置生效
expose_apply() {
  if pid_of >/dev/null 2>&1; then
    log "网关正在运行，重启以应用新监听配置..."
    cmd_stop
    cmd_start
  else
    log "网关未运行；下次 ./run.sh start 时生效"
  fi
}

cmd_expose() {
  local sub="${1:-}"
  shift || true
  case "$sub" in
    on|open)
      load_env
      save_env_value ANTIGRAVITY_HOST "0.0.0.0"
      export ANTIGRAVITY_HOST="0.0.0.0"
      local key="${1:-${ANTIGRAVITY_API_KEY:-}}"
      if [[ -z "$key" ]]; then
        key="$(gen_api_key)"
        log "未指定密钥，已随机生成"
      elif ((${#key} < 8)); then
        log "密钥太短（${#key} 位），已替换为随机强密钥"
        key="$(gen_api_key)"
      fi
      save_env_value ANTIGRAVITY_API_KEY "$key"
      export ANTIGRAVITY_API_KEY="$key"
      log "已写入 $ENV_FILE：ANTIGRAVITY_HOST=0.0.0.0"
      log "网关密钥 ANTIGRAVITY_API_KEY=${key}"
      log "调用方请求头：Authorization: Bearer ${key}"
      log "（HeySure 模型预设里 API Key 填这个值）"
      log "安全提醒：0.0.0.0 含公网网卡。若只想给本机 Docker 容器用，"
      log "请在云安全组/防火墙里保持 ${ANTIGRAVITY_PORT:-8110} 端口对公网关闭。"
      expose_apply
      ;;
    off|local|close)
      load_env
      save_env_value ANTIGRAVITY_HOST "127.0.0.1"
      export ANTIGRAVITY_HOST="127.0.0.1"
      log "已改回仅本机监听（127.0.0.1）"
      expose_apply
      ;;
    show|status)
      expose_show
      ;;
    "")
      expose_show
      echo
      echo "  1) 对外开放（0.0.0.0 + 自动生成网关密钥）"
      echo "  2) 对外开放（0.0.0.0 + 手动输入密钥）"
      echo "  3) 收回仅本机（127.0.0.1）"
      echo "  0) 返回"
      if [[ ! -t 0 ]] || [[ ! -t 1 ]]; then
        die "非交互环境请用：$0 expose on [密钥] / off / show"
      fi
      printf '请选择 > '
      local c
      read -r c || return 0
      case "$c" in
        1) cmd_expose on ;;
        2)
          local k
          printf '输入网关密钥（≥8 位）> '
          read -r k
          cmd_expose on "$k"
          ;;
        3) cmd_expose off ;;
        0|"") return 0 ;;
        *) log "无效选项：$c" ;;
      esac
      ;;
    *)
      cat <<'EOF'
用法: ./run.sh expose [子命令]

  （无参数）     交互选择开放 / 收回
  on [密钥]      对外开放：监听 0.0.0.0 并强制设置网关密钥
                 （不给密钥则自动生成；写入 .env，重启后仍生效）
  off            收回仅本机监听 127.0.0.1
  show           显示当前监听范围与密钥状态

说明:
  - 对外开放后调用方必须带 Authorization: Bearer <密钥>。
  - 只想给本机 Docker 容器用时：expose on 即可，同时在云安全组里
    保持 8110 端口对公网关闭（容器经宿主机网桥访问，不走公网）。
EOF
      exit 1
      ;;
  esac
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
  echo "工具协议：文本 MCP Call"
  echo "认证存储：官方 agy 当前运行用户的本地凭据（不在代码目录）"
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

resolve_agy() {
  local candidate
  for candidate in \
    "${ANTIGRAVITY_CLI_COMMAND:-}" \
    "${RUN_USER_HOME:-}/.local/bin/agy" \
    "${RUN_USER_HOME:-}/.antigravity/bin/agy" \
    /usr/local/bin/agy /usr/bin/agy; do
    [[ -n "$candidate" && "$candidate" != "agy" && -x "$candidate" ]] || continue
    printf '%s' "$candidate"
    return 0
  done
  if command -v agy >/dev/null 2>&1; then
    command -v agy
    return 0
  fi
  return 1
}

ensure_cli_command() {
  [[ "$ANTIGRAVITY_BACKEND" == "cli" ]] || return 0
  local resolved
  resolved="$(resolve_agy 2>/dev/null || true)"
  [[ -n "$resolved" ]] || die "未找到官方 Antigravity CLI（agy）；请先运行 $0 install-cli"
  ANTIGRAVITY_CLI_COMMAND="$resolved"
  export ANTIGRAVITY_CLI_COMMAND
}

cmd_install_cli() {
  load_env
  command -v "$PYTHON_BIN" >/dev/null 2>&1 || cmd_deps
  command -v curl >/dev/null 2>&1 || cmd_deps
  prepare_runtime
  local installer="$RUNTIME_DIR/install-agy.sh"
  log "正在下载 Google 官方 Antigravity CLI 安装器……"
  curl -fsSL https://antigravity.google/cli/install.sh -o "$installer" \
    || die "下载官方 agy 安装器失败；请检查网络或代理"
  chmod 0755 "$installer"
  if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
    chown "$RUN_USER_NAME":"$RUN_USER_NAME" "$installer"
  fi
  log "将以普通用户 $RUN_USER_NAME 安装，CLI 与登录数据均位于该用户目录"
  run_as_service_user bash "$installer"
  rm -f "$installer"
  ANTIGRAVITY_CLI_COMMAND="${RUN_USER_HOME}/.local/bin/agy"
  export ANTIGRAVITY_CLI_COMMAND
  ensure_cli_command
  log "安装完成：$ANTIGRAVITY_CLI_COMMAND"
}

run_as_service_user() {
  local -a forwarded
  forwarded=(
    "ANTIGRAVITY_HOST=$ANTIGRAVITY_HOST"
    "ANTIGRAVITY_PORT=$ANTIGRAVITY_PORT"
    "ANTIGRAVITY_TIMEOUT=$ANTIGRAVITY_TIMEOUT"
    "ANTIGRAVITY_BACKEND=$ANTIGRAVITY_BACKEND"
    "ANTIGRAVITY_CLI_COMMAND=$ANTIGRAVITY_CLI_COMMAND"
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
    env PATH="${RUN_USER_HOME}/.local/bin:$PATH" "${forwarded[@]}" "$@"
  else
    runuser -u "$RUN_USER_NAME" -- env \
      HOME="$RUN_USER_HOME" USER="$RUN_USER_NAME" LOGNAME="$RUN_USER_NAME" PATH="${RUN_USER_HOME}/.local/bin:$PATH" \
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
  prepare_runtime
  if [[ "$ANTIGRAVITY_BACKEND" == "cli" ]]; then
    if ! resolve_agy >/dev/null 2>&1; then
      log "尚未安装 agy，先自动安装官方 Antigravity CLI"
      cmd_install_cli
      prepare_runtime
    fi
    ensure_cli_command
    log "即将启动官方 agy 登录（运行用户：$RUN_USER_NAME）"
    log "SSH 服务器会显示授权网址：在自己电脑浏览器打开，登录 Google 后把授权码粘贴回来即可。"
    log "账号 token 由 agy 保存在 $RUN_USER_NAME 的本地用户数据中，不写入源码、.env 或 Git。"
    run_as_service_user "$PYTHON_BIN" "$SERVER_FILE" login --backend cli
  else
    ensure_oauth_client
    log "兼容 direct 模式：将使用普通用户 $RUN_USER_NAME 保存 OAuth 凭证"
    run_as_service_user "$PYTHON_BIN" "$SERVER_FILE" login --backend direct \
      --auth-file "$AUTH_FILE" --callback-port "${ANTIGRAVITY_OAUTH_CALLBACK_PORT:-51121}" --no-browser
  fi
  ensure_gateway_api_key
  print_heysure_config
}

cmd_auth_status() {
  load_env
  prepare_runtime
  ensure_cli_command
  run_as_service_user "$PYTHON_BIN" "$SERVER_FILE" auth-status \
    --backend "$ANTIGRAVITY_BACKEND" --auth-file "$AUTH_FILE"
}

require_auth() {
  if [[ "$ANTIGRAVITY_BACKEND" == "cli" ]]; then
    ensure_cli_command
    run_as_service_user "$PYTHON_BIN" "$SERVER_FILE" auth-status --backend cli >/dev/null 2>&1 \
      || die "agy 尚未登录或登录已失效；请先运行 $0 login"
  else
    [[ -f "$AUTH_FILE" ]] || die "尚未登录；请先运行 $0 login"
  fi
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
    nohup "$1" "$2" serve --backend "$3" --auth-file "$4" >> "$5" 2>&1 </dev/null &
    echo $!
  ' bash "$PYTHON_BIN" "$SERVER_FILE" "$ANTIGRAVITY_BACKEND" "$AUTH_FILE" "$LOG_FILE")"
  [[ "$gateway_pid" =~ ^[0-9]+$ ]] || die "未取得网关进程 PID：$gateway_pid"
  echo "$gateway_pid" > "$PID_FILE"
  sleep 1
  pid_of >/dev/null || { tail -n 30 "$LOG_FILE" >&2 || true; die "网关启动失败"; }
  log "已启动：http://$(local_probe_host):${ANTIGRAVITY_PORT}/v1/chat/completions（监听 ${ANTIGRAVITY_HOST}）"
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
    local owner probe
    owner="$(ps -o user= -p "$pid" 2>/dev/null | xargs || true)"
    probe="$(local_probe_host)"
    log "运行中（pid $pid，用户 ${owner:-unknown}）监听 ${ANTIGRAVITY_HOST}:${ANTIGRAVITY_PORT} → 探测 http://${probe}:${ANTIGRAVITY_PORT}/"
    curl -fsS "http://${probe}:${ANTIGRAVITY_PORT}/health" 2>/dev/null || true
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
  run_as_service_user "$PYTHON_BIN" "$SERVER_FILE" serve \
    --backend "$ANTIGRAVITY_BACKEND" --auth-file "$AUTH_FILE"
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
    load_env
    echo
    echo "========== Antigravity Python 网关 =========="
    echo "  1) 检查 / 自动安装 Python 依赖"
    echo "  2) 安装 / 更新官方 Antigravity CLI (agy)"
    echo "  3) 直接登录 Google 账号（由 agy 本地保存）"
    echo "  4) 查看 agy 登录状态 / 可用模型"
    echo "  5) 显示 HeySure 模型配置"
    echo "  6) 配置代理"
    echo "  7) 启动网关"
    echo "  8) 停止网关"
    echo "  9) 重启网关"
    echo "  s) 查看服务状态"
    echo "  e) 对外开放/收回 (expose) 当前监听: ${ANTIGRAVITY_HOST:-127.0.0.1}"
    echo "  l) 查看最近日志"
    echo "  a) 前台启动调试"
    echo "  0) 退出"
    echo "=============================================="
    printf '请选择 > '
    local choice
    read -r choice || return 0
    case "$choice" in
      1) cmd_deps ;;
      2) cmd_install_cli || true ;;
      3) cmd_login || true ;;
      4) cmd_auth_status || true ;;
      5) cmd_config ;;
      6) cmd_proxy_interactive ;;
      7) cmd_start ;;
      8) cmd_stop ;;
      9) cmd_stop; cmd_start ;;
      s|S) cmd_status || true ;;
      e|E) cmd_expose ;;
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
  install-cli          安装或更新 Google 官方 Antigravity CLI (agy)
  login                直接运行 agy 登录 Google（凭据保存在运行用户本地）
  auth-status          用 agy models 检查登录并显示账号可用模型
  config               生成/显示 HeySure 要填写的模型、URL 和本地 API Key
  proxy <url>|clear    配置或清除代理
  expose               对外开放（0.0.0.0 + 强制网关密钥）/ 收回本机监听
    expose on [密钥] | off | show
  start|stop|restart   后台启停 Python 网关
  status               查看状态和健康检查
  logs                 最近 100 行日志
  logs -f              持续查看日志
  fg                   前台运行
  fix-crlf             去除本目录 .sh/.py 的 Windows CRLF（也可 python3 server.py fix-crlf）

无参数直接运行时进入交互菜单。root 仅负责部署和管理；agy 登录及网关默认以
普通用户 antigravity-api 运行，用户 token 不保存到代码目录。
可用 ANTIGRAVITY_RUN_USER 指定已有普通用户。

若 ./run.sh 报 bash\r：请执行  bash run.sh  一次（会自动修 CRLF 并继续）。

若需 Docker 容器/局域网/公网访问网关（写入 .env，重启后仍生效）：
  ./run.sh expose on
  ./run.sh expose off
  ./run.sh expose show
EOF
}

cmd_fix_crlf() {
  # 优先用当前目录源码（不依赖 SERVICE_DIR 副本）
  if command -v "$PYTHON_BIN" >/dev/null 2>&1 || command -v python3 >/dev/null 2>&1; then
    local py="${PYTHON_BIN}"
    command -v "$py" >/dev/null 2>&1 || py=python3
    "$py" "$ROOT/server.py" fix-crlf || true
  fi
  local f changed=0
  for f in "$ROOT"/run.sh "$ROOT"/*.sh "$ROOT"/*.py; do
    [[ -f "$f" ]] || continue
    if grep -q $'\r' "$f" 2>/dev/null; then
      tr -d '\r' < "$f" > "${f}.$$.lf"
      mv -f "${f}.$$.lf" "$f"
      [[ "$f" == *.sh ]] && chmod +x "$f" 2>/dev/null || true
      log "已去除 Windows CRLF：$f"
      changed=1
    fi
  done
  [[ "$changed" -eq 0 ]] && log "无需修复（未发现 CRLF）"
}

case "${1:-}" in
  "") menu ;;
  deps) cmd_deps ;;
  install-cli|install_cli|install) cmd_install_cli ;;
  login) cmd_login ;;
  auth-status) cmd_auth_status ;;
  config) cmd_config ;;
  proxy) shift; cmd_proxy "$@" ;;
  expose|open) shift || true; cmd_expose "$@" ;;
  start) cmd_start ;;
  stop) cmd_stop ;;
  restart) cmd_stop; cmd_start ;;
  status) cmd_status ;;
  logs) shift; if [[ "${1:-}" == "-f" ]]; then ensure_log_file; tail -f "$LOG_FILE"; else cmd_logs; fi ;;
  fg) cmd_fg ;;
  fix-crlf|fixcrlf|crlf) cmd_fix_crlf ;;
  help|-h|--help) usage ;;
  *) log "未知命令：$1"; usage; exit 1 ;;
esac
