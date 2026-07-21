#!/usr/bin/env bash
# grok_cli 服务器管理脚本（Linux）
#
# 用法：
#   chmod +x run.sh
#   ./run.sh                 # 交互菜单
#   ./run.sh deps            # 安装系统依赖（python3 / curl 等）
#   ./run.sh install-cli     # 安装 / 更新 grok CLI
#   ./run.sh login           # 检查登录；未登录则引导登录
#   ./run.sh expose on|off   # 对外开放（0.0.0.0 + 网关密钥）/ 收回本机
#   ./run.sh start|stop|restart|status|logs
#   ./run.sh fg              # 前台启动（调试用）
#
# 环境变量（可选，与 server.py 一致）：
#   GROK_CLI_COMMAND  CLI 路径（默认自动探测 ~/.grok/bin/grok 或 PATH 中的 grok）
#   GROK_CLI_HOST     默认 127.0.0.1；对外暴露可设 0.0.0.0
#   GROK_CLI_PORT     默认 8100
#   GROK_CLI_TIMEOUT  默认 600
#   GROK_CLI_API_KEY  网关鉴权（可选）
#   GROK_CLI_MODELS   默认 grok-4.5
#   XAI_API_KEY       无浏览器环境可用 API Key 登录 grok CLI
#   PYTHON            python 解释器，默认 python3
#   代理（也可 ./run.sh proxy 交互配置，写入 .env.proxy）：
#   GROK_CLI_PROXY_HOST / GROK_CLI_PROXY_PORT / GROK_CLI_PROXY_SCHEME
#   GROK_CLI_PROXY_URL  完整代理地址，如 http://127.0.0.1:7890
#   GROK_CLI_PROXY_USER / GROK_CLI_PROXY_PASS  可选认证

set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

RUNTIME_DIR="${ROOT}/runtime"
PID_FILE="${RUNTIME_DIR}/gateway.pid"
LOG_FILE="${RUNTIME_DIR}/gateway.log"
PROXY_ENV_FILE="${ROOT}/.env.proxy"
ENV_FILE="${ROOT}/.env"
PYTHON="${PYTHON:-python3}"

# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

log()  { printf '[grok_cli] %s\n' "$*"; }
err()  { printf '[grok_cli] ERROR: %s\n' "$*" >&2; }
die()  { err "$*"; exit 1; }

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || return 1
}

ensure_runtime() {
  mkdir -p "$RUNTIME_DIR"
}

# 解析 grok CLI 可执行路径（写入 GROK_CLI_COMMAND 若未设置）
resolve_grok() {
  if [[ -n "${GROK_CLI_COMMAND:-}" ]]; then
    # 允许 "path with spaces" 或完整路径
    local first
    first="$(printf '%s' "$GROK_CLI_COMMAND" | awk '{print $1}')"
    if [[ -x "$first" ]] || need_cmd "$first" || [[ -f "$first" ]]; then
      printf '%s' "$GROK_CLI_COMMAND"
      return 0
    fi
  fi

  local candidates=(
    "${HOME}/.grok/bin/grok"
    "${HOME}/.local/bin/grok"
    "/usr/local/bin/grok"
    "/usr/bin/grok"
  )
  local p
  for p in "${candidates[@]}"; do
    if [[ -x "$p" ]]; then
      printf '%s' "$p"
      return 0
    fi
  done
  if need_cmd grok; then
    command -v grok
    return 0
  fi
  return 1
}

export_defaults() {
  export GROK_CLI_HOST="${GROK_CLI_HOST:-127.0.0.1}"
  export GROK_CLI_PORT="${GROK_CLI_PORT:-8100}"
  export GROK_CLI_TIMEOUT="${GROK_CLI_TIMEOUT:-600}"
  export GROK_CLI_MODELS="${GROK_CLI_MODELS:-grok-4.5}"
  if [[ -z "${GROK_CLI_COMMAND:-}" ]]; then
    if cmd="$(resolve_grok 2>/dev/null)"; then
      export GROK_CLI_COMMAND="$cmd"
    fi
  fi
  # 根据 host/port 或完整 URL 导出标准代理环境变量
  apply_proxy_env
}

# ---------------------------------------------------------------------------
# 系统 HTTP(S)/SOCKS 代理
# ---------------------------------------------------------------------------

# 从环境变量拼出代理 URL；无配置则返回 1
build_proxy_url() {
  local url="${GROK_CLI_PROXY_URL:-}"
  if [[ -n "$url" ]]; then
    # 补全 scheme
    if [[ "$url" != *"://"* ]]; then
      url="http://${url}"
    fi
    printf '%s' "$url"
    return 0
  fi

  local host="${GROK_CLI_PROXY_HOST:-}"
  local port="${GROK_CLI_PROXY_PORT:-}"
  local scheme="${GROK_CLI_PROXY_SCHEME:-http}"
  local user="${GROK_CLI_PROXY_USER:-}"
  local pass="${GROK_CLI_PROXY_PASS:-}"

  [[ -n "$host" ]] || return 1
  [[ -n "$port" ]] || return 1

  scheme="${scheme,,}"
  case "$scheme" in
    http|https|socks5|socks5h|socks4) ;;
    *) scheme="http" ;;
  esac

  local auth=""
  if [[ -n "$user" ]]; then
    # URL 编码尽量简单处理：空格等少见字符由用户避免
    auth="${user}"
    if [[ -n "$pass" ]]; then
      auth="${auth}:${pass}"
    fi
    auth="${auth}@"
  fi

  printf '%s://%s%s:%s' "$scheme" "$auth" "$host" "$port"
}

# 清除当前 shell 中的代理相关变量
clear_proxy_env_vars() {
  unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy \
    NO_PROXY no_proxy \
    GROK_CLI_PROXY_URL GROK_CLI_PROXY_HOST GROK_CLI_PROXY_PORT \
    GROK_CLI_PROXY_SCHEME GROK_CLI_PROXY_USER GROK_CLI_PROXY_PASS \
    2>/dev/null || true
}

# 将配置导出为进程环境（curl / python / grok 子进程会继承）
apply_proxy_env() {
  local url=""
  if ! url="$(build_proxy_url 2>/dev/null)"; then
    return 0
  fi
  [[ -n "$url" ]] || return 0

  export GROK_CLI_PROXY_URL="$url"
  export http_proxy="$url"
  export https_proxy="$url"
  export HTTP_PROXY="$url"
  export HTTPS_PROXY="$url"
  export all_proxy="$url"
  export ALL_PROXY="$url"

  local nop="localhost,127.0.0.1,::1,${GROK_CLI_HOST:-127.0.0.1}"
  if [[ -n "${GROK_CLI_NO_PROXY:-}" ]]; then
    nop="${GROK_CLI_NO_PROXY}"
  fi
  export no_proxy="$nop"
  export NO_PROXY="$nop"
}

# 掩码显示代理（隐藏密码）
proxy_display() {
  local url=""
  if ! url="$(build_proxy_url 2>/dev/null)"; then
    printf '(未配置)'
    return 0
  fi
  # user:pass@ -> user:***@
  url="$(printf '%s' "$url" | sed -E 's#(://[^:/@]+):[^@/]+@#\1:***@#')"
  printf '%s' "$url"
}

save_proxy_file() {
  local host="${GROK_CLI_PROXY_HOST:-}"
  local port="${GROK_CLI_PROXY_PORT:-}"
  local scheme="${GROK_CLI_PROXY_SCHEME:-http}"
  local user="${GROK_CLI_PROXY_USER:-}"
  local pass="${GROK_CLI_PROXY_PASS:-}"
  local url="${GROK_CLI_PROXY_URL:-}"
  local nop="${GROK_CLI_NO_PROXY:-localhost,127.0.0.1,::1}"

  umask 077
  {
    echo "# grok_cli 代理配置 — 由 ./run.sh proxy 生成，start/install-cli 会自动加载"
    echo "# 也可手动 export 后启动；本文件优先于空环境"
    if [[ -n "$url" ]]; then
      printf 'export GROK_CLI_PROXY_URL=%q\n' "$url"
    fi
    if [[ -n "$host" ]]; then
      printf 'export GROK_CLI_PROXY_HOST=%q\n' "$host"
    fi
    if [[ -n "$port" ]]; then
      printf 'export GROK_CLI_PROXY_PORT=%q\n' "$port"
    fi
    printf 'export GROK_CLI_PROXY_SCHEME=%q\n' "$scheme"
    if [[ -n "$user" ]]; then
      printf 'export GROK_CLI_PROXY_USER=%q\n' "$user"
    fi
    if [[ -n "$pass" ]]; then
      printf 'export GROK_CLI_PROXY_PASS=%q\n' "$pass"
    fi
    printf 'export GROK_CLI_NO_PROXY=%q\n' "$nop"
  } > "$PROXY_ENV_FILE"
  chmod 600 "$PROXY_ENV_FILE"

  # 立即应用并写回标准代理变量到文件，方便 source
  apply_proxy_env
  if url="$(build_proxy_url 2>/dev/null)"; then
    {
      echo ""
      echo "# 标准代理环境变量（供 curl / grok / python urllib 使用）"
      printf 'export http_proxy=%q\n' "$url"
      printf 'export https_proxy=%q\n' "$url"
      printf 'export HTTP_PROXY=%q\n' "$url"
      printf 'export HTTPS_PROXY=%q\n' "$url"
      printf 'export all_proxy=%q\n' "$url"
      printf 'export ALL_PROXY=%q\n' "$url"
      printf 'export no_proxy=%q\n' "${NO_PROXY:-$nop}"
      printf 'export NO_PROXY=%q\n' "${NO_PROXY:-$nop}"
    } >> "$PROXY_ENV_FILE"
  fi
  log "已保存代理配置 → $PROXY_ENV_FILE"
  log "当前代理：$(proxy_display)"
}

proxy_show() {
  load_optional_env
  apply_proxy_env
  echo "---- 代理配置 ----"
  if [[ -f "$PROXY_ENV_FILE" ]]; then
    echo "文件     : $PROXY_ENV_FILE"
  else
    echo "文件     : (无 .env.proxy)"
  fi
  echo "HOST     : ${GROK_CLI_PROXY_HOST:-(空)}"
  echo "PORT     : ${GROK_CLI_PROXY_PORT:-(空)}"
  echo "SCHEME   : ${GROK_CLI_PROXY_SCHEME:-http}"
  echo "USER     : ${GROK_CLI_PROXY_USER:-(无)}"
  echo "URL      : $(proxy_display)"
  echo "NO_PROXY : ${NO_PROXY:-${GROK_CLI_NO_PROXY:-localhost,127.0.0.1,::1}}"
  echo "------------------"
}

proxy_clear() {
  clear_proxy_env_vars
  if [[ -f "$PROXY_ENV_FILE" ]]; then
    rm -f "$PROXY_ENV_FILE"
    log "已删除 $PROXY_ENV_FILE 并清除当前会话代理变量"
  else
    log "无已保存的代理配置；已清除当前会话代理变量"
  fi
}

proxy_test() {
  load_optional_env
  apply_proxy_env
  local url
  if ! url="$(build_proxy_url 2>/dev/null)"; then
    die "未配置代理。请先：./run.sh proxy"
  fi
  need_cmd curl || die "需要 curl"
  log "使用代理 $(proxy_display) 探测外网..."
  # 不走代理测本机；走代理测公共 HTTPS
  if curl -fsS --max-time 15 -o /dev/null -w "HTTP %{http_code}  time %{time_total}s\n" \
      https://api.x.ai/ 2>/dev/null \
    || curl -fsS --max-time 15 -o /dev/null -w "HTTP %{http_code}  time %{time_total}s\n" \
      https://x.ai/ 2>/dev/null \
    || curl -fsS --max-time 15 -o /dev/null -w "HTTP %{http_code}  time %{time_total}s\n" \
      https://www.google.com/generate_204 2>/dev/null; then
    log "代理连通性：OK"
  else
    err "代理探测失败。请检查网址/端口、协议(http/socks5)、账号密码与防火墙。"
    err "可手动：curl -x $(proxy_display) -I https://x.ai/"
    exit 1
  fi
}

# 解析 host:port 或完整 URL
parse_proxy_input() {
  local raw="$1"
  raw="$(printf '%s' "$raw" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
  [[ -n "$raw" ]] || return 1

  # 已是完整 URL
  if [[ "$raw" == *"://"* ]]; then
    export GROK_CLI_PROXY_URL="$raw"
    # 尽量拆 host/port 便于展示
    local rest scheme
    scheme="${raw%%://*}"
    rest="${raw#*://}"
    export GROK_CLI_PROXY_SCHEME="$scheme"
    # 去掉 userinfo
    if [[ "$rest" == *"@"* ]]; then
      local userinfo
      userinfo="${rest%%@*}"
      rest="${rest#*@}"
      if [[ "$userinfo" == *":"* ]]; then
        export GROK_CLI_PROXY_USER="${userinfo%%:*}"
        export GROK_CLI_PROXY_PASS="${userinfo#*:}"
      else
        export GROK_CLI_PROXY_USER="$userinfo"
      fi
    fi
    # 去掉 path
    rest="${rest%%/*}"
    if [[ "$rest" == *"]:"* ]]; then
      # [ipv6]:port
      export GROK_CLI_PROXY_HOST="${rest%:*}"
      export GROK_CLI_PROXY_PORT="${rest##*:}"
    elif [[ "$rest" == *":"* ]]; then
      export GROK_CLI_PROXY_HOST="${rest%:*}"
      export GROK_CLI_PROXY_PORT="${rest##*:}"
    else
      export GROK_CLI_PROXY_HOST="$rest"
    fi
    return 0
  fi

  # host:port
  if [[ "$raw" == *":"* ]]; then
    export GROK_CLI_PROXY_HOST="${raw%:*}"
    export GROK_CLI_PROXY_PORT="${raw##*:}"
    unset GROK_CLI_PROXY_URL 2>/dev/null || true
    return 0
  fi

  # 仅 host
  export GROK_CLI_PROXY_HOST="$raw"
  unset GROK_CLI_PROXY_URL 2>/dev/null || true
  return 0
}

proxy_set_from_args() {
  # ./run.sh proxy set --host x --port 7890 [--scheme http] [--user u] [--pass p]
  # ./run.sh proxy set --url http://127.0.0.1:7890
  local host="" port="" scheme="http" user="" pass="" url="" nop=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --host|-H) host="${2:-}"; shift 2 ;;
      --port|-P) port="${2:-}"; shift 2 ;;
      --scheme|-s) scheme="${2:-}"; shift 2 ;;
      --user|-u) user="${2:-}"; shift 2 ;;
      --pass|-p) pass="${2:-}"; shift 2 ;;
      --url) url="${2:-}"; shift 2 ;;
      --no-proxy) nop="${2:-}"; shift 2 ;;
      *)
        # 位置参数：host:port 或 url
        if [[ -z "$url" && -z "$host" ]]; then
          parse_proxy_input "$1" || true
          host="${GROK_CLI_PROXY_HOST:-$host}"
          port="${GROK_CLI_PROXY_PORT:-$port}"
          url="${GROK_CLI_PROXY_URL:-$url}"
          scheme="${GROK_CLI_PROXY_SCHEME:-$scheme}"
        fi
        shift
        ;;
    esac
  done

  if [[ -n "$url" ]]; then
    parse_proxy_input "$url"
  else
    [[ -n "$host" ]] || die "请指定 --host 与 --port，或 --url / host:port"
    [[ -n "$port" ]] || die "请指定 --port（代理端口）"
    export GROK_CLI_PROXY_HOST="$host"
    export GROK_CLI_PROXY_PORT="$port"
    export GROK_CLI_PROXY_SCHEME="${scheme:-http}"
    unset GROK_CLI_PROXY_URL 2>/dev/null || true
  fi
  if [[ -n "$user" ]]; then
    export GROK_CLI_PROXY_USER="$user"
  fi
  if [[ -n "$pass" ]]; then
    export GROK_CLI_PROXY_PASS="$pass"
  fi
  if [[ -n "$nop" ]]; then
    export GROK_CLI_NO_PROXY="$nop"
  fi
  # 校验端口
  port="${GROK_CLI_PROXY_PORT:-}"
  if [[ -n "$port" ]]; then
    if ! [[ "$port" =~ ^[0-9]+$ ]] || ((port < 1 || port > 65535)); then
      die "无效端口：$port（应为 1-65535）"
    fi
  fi
  save_proxy_file
}

proxy_interactive() {
  load_optional_env
  echo
  echo "当前代理：$(proxy_display)"
  echo
  echo "  1) 设置代理（输入网址/IP + 端口）"
  echo "  2) 用完整 URL 设置（如 http://127.0.0.1:7890 或 socks5://...）"
  echo "  3) 测试代理连通性"
  echo "  4) 清除代理"
  echo "  5) 显示当前配置"
  echo "  0) 返回"
  echo -n "请选择 > "
  local c
  read -r c || return 0
  case "$c" in
    1)
      local host port scheme user pass
      echo -n "代理地址（IP 或域名，不要带端口）> "
      read -r host
      [[ -n "$host" ]] || die "地址不能为空"
      echo -n "代理端口（如 7890 / 1080 / 8080）> "
      read -r port
      [[ -n "$port" ]] || die "端口不能为空"
      if ! [[ "$port" =~ ^[0-9]+$ ]] || ((port < 1 || port > 65535)); then
        die "无效端口：$port"
      fi
      echo -n "协议 [http/https/socks5，默认 http] > "
      read -r scheme
      scheme="${scheme:-http}"
      echo -n "用户名（可选，直接回车跳过）> "
      read -r user
      if [[ -n "$user" ]]; then
        echo -n "密码（可选）> "
        if [[ -t 0 ]]; then
          stty -echo 2>/dev/null || true
          read -r pass
          stty echo 2>/dev/null || true
          echo
        else
          read -r pass
        fi
      fi
      clear_proxy_env_vars
      export GROK_CLI_PROXY_HOST="$host"
      export GROK_CLI_PROXY_PORT="$port"
      export GROK_CLI_PROXY_SCHEME="$scheme"
      [[ -n "$user" ]] && export GROK_CLI_PROXY_USER="$user"
      [[ -n "${pass:-}" ]] && export GROK_CLI_PROXY_PASS="$pass"
      unset GROK_CLI_PROXY_URL 2>/dev/null || true
      save_proxy_file
      echo -n "是否立刻测试连通性？[Y/n] > "
      local t
      read -r t
      if [[ -z "$t" || "$t" == [Yy]* ]]; then
        proxy_test || true
      fi
      if is_running; then
        echo -n "网关正在运行，是否重启以应用代理？[Y/n] > "
        read -r t
        if [[ -z "$t" || "$t" == [Yy]* ]]; then
          cmd_restart
        fi
      fi
      ;;
    2)
      local full
      echo -n "完整代理 URL > "
      read -r full
      [[ -n "$full" ]] || die "URL 不能为空"
      clear_proxy_env_vars
      parse_proxy_input "$full" || die "无法解析：$full"
      save_proxy_file
      echo -n "是否立刻测试连通性？[Y/n] > "
      local t
      read -r t
      if [[ -z "$t" || "$t" == [Yy]* ]]; then
        proxy_test || true
      fi
      if is_running; then
        echo -n "网关正在运行，是否重启以应用代理？[Y/n] > "
        read -r t
        if [[ -z "$t" || "$t" == [Yy]* ]]; then
          cmd_restart
        fi
      fi
      ;;
    3) proxy_test ;;
    4) proxy_clear ;;
    5) proxy_show ;;
    0|"") return 0 ;;
    *) err "无效选项" ;;
  esac
}

cmd_proxy() {
  local sub="${1:-}"
  shift || true
  case "$sub" in
    ""|menu|config)
      if [[ -t 0 && -t 1 ]]; then
        proxy_interactive
      else
        proxy_show
        err "非交互环境请用：proxy set --host HOST --port PORT"
        exit 1
      fi
      ;;
    set)
      proxy_set_from_args "$@"
      ;;
    show|status|get)
      proxy_show
      ;;
    clear|off|unset|disable)
      proxy_clear
      ;;
    test|check)
      proxy_test
      ;;
    *)
      cat <<'EOF'
用法: ./run.sh proxy [子命令]

  （无参数）     交互配置代理网址与端口
  set            非交互设置
      --host HOST --port PORT [--scheme http|https|socks5]
      [--user USER] [--pass PASS]
      --url http://HOST:PORT
      或: ./run.sh proxy set 127.0.0.1:7890
  show           显示当前代理
  test           测试代理连通性
  clear          清除代理配置

示例:
  ./run.sh proxy set --host 127.0.0.1 --port 7890
  ./run.sh proxy set --url socks5://127.0.0.1:1080
  ./run.sh proxy test
  ./run.sh proxy clear
EOF
      [[ -z "$sub" ]] || exit 1
      ;;
  esac
}

is_running() {
  if [[ ! -f "$PID_FILE" ]]; then
    return 1
  fi
  local pid
  pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  [[ -n "$pid" ]] || return 1
  if kill -0 "$pid" 2>/dev/null; then
    return 0
  fi
  # 残留 pid 文件
  rm -f "$PID_FILE"
  return 1
}

pid_of() {
  [[ -f "$PID_FILE" ]] && cat "$PID_FILE" || true
}

# ---------------------------------------------------------------------------
# deps — 安装系统依赖（网关本身无 pip 依赖）
# ---------------------------------------------------------------------------

cmd_deps() {
  log "检查 / 安装系统依赖（python3、curl、ca-certificates）..."

  local missing=()
  need_cmd "$PYTHON" || missing+=("python3")
  need_cmd curl || missing+=("curl")

  if ((${#missing[@]} == 0)); then
    log "已满足：$(command -v "$PYTHON")、$(command -v curl)"
    "$PYTHON" -c 'import sys; print("Python", sys.version.split()[0])'
    # 验证标准库 http.server 可用
    "$PYTHON" -c 'import http.server, json, urllib.request' \
      || die "当前 Python 缺少标准库，请重装 python3"
    log "依赖检查通过（server.py 仅需 Python 标准库）"
    return 0
  fi

  log "缺少：${missing[*]}"
  if [[ "$(id -u)" -ne 0 ]]; then
    err "需要 root 安装系统包，请执行："
    if need_cmd apt-get; then
      err "  sudo apt-get update && sudo apt-get install -y python3 curl ca-certificates"
    elif need_cmd dnf; then
      err "  sudo dnf install -y python3 curl ca-certificates"
    elif need_cmd yum; then
      err "  sudo yum install -y python3 curl ca-certificates"
    elif need_cmd apk; then
      err "  sudo apk add python3 curl ca-certificates"
    else
      err "  请手动安装：python3 curl ca-certificates"
    fi
    err "或：sudo $0 deps"
    exit 1
  fi

  if need_cmd apt-get; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -y
    apt-get install -y python3 curl ca-certificates
  elif need_cmd dnf; then
    dnf install -y python3 curl ca-certificates
  elif need_cmd yum; then
    yum install -y python3 curl ca-certificates
  elif need_cmd apk; then
    apk add --no-cache python3 curl ca-certificates
  else
    die "无法识别包管理器，请手动安装 python3 与 curl"
  fi

  need_cmd "$PYTHON" || die "安装后仍找不到 $PYTHON"
  need_cmd curl || die "安装后仍找不到 curl"
  log "系统依赖安装完成"
}

# ---------------------------------------------------------------------------
# install-cli — 安装 / 更新官方 grok CLI
# ---------------------------------------------------------------------------

cmd_install_cli() {
  load_optional_env
  need_cmd curl || die "需要 curl，请先执行：$0 deps"
  if build_proxy_url >/dev/null 2>&1; then
    log "安装将走代理：$(proxy_display)"
  fi
  log "安装 / 更新 grok CLI（官方脚本：https://x.ai/cli/install.sh）..."
  curl -fsSL https://x.ai/cli/install.sh | bash

  # 安装脚本通常装到 ~/.grok/bin/grok，可能未进 PATH
  local bin="${HOME}/.grok/bin"
  if [[ -d "$bin" ]]; then
    case ":${PATH}:" in
      *":${bin}:"*) ;;
      *) export PATH="${bin}:${PATH}" ;;
    esac
  fi

  if cmd="$(resolve_grok 2>/dev/null)"; then
    export GROK_CLI_COMMAND="$cmd"
    log "grok CLI 已就绪：$cmd"
    "$cmd" --version 2>/dev/null || "$cmd" -v 2>/dev/null || true
  else
    err "安装脚本已执行，但未在常见路径找到 grok。"
    err "请确认安装输出，或手动设置：export GROK_CLI_COMMAND=/path/to/grok"
    exit 1
  fi
}

# ---------------------------------------------------------------------------
# login — 检查登录；未登录则引导
# ---------------------------------------------------------------------------

auth_file_present() {
  local f
  for f in \
    "${HOME}/.grok/auth.json" \
    "${HOME}/.grok/credentials.json" \
    "${HOME}/.config/grok/auth.json"
  do
    if [[ -f "$f" ]] && [[ -s "$f" ]]; then
      printf '%s' "$f"
      return 0
    fi
  done
  return 1
}

# 返回 0 = 已登录 / 有可用凭证
is_logged_in() {
  if [[ -n "${XAI_API_KEY:-}" ]]; then
    return 0
  fi
  if auth_file_present >/dev/null; then
    return 0
  fi
  return 1
}

# 轻量探测：headless 发一句极短 prompt；失败不致命
probe_cli_auth() {
  local grok="$1"
  # 部分版本支持 auth status / whoami
  if "$grok" auth status >/dev/null 2>&1; then
    return 0
  fi
  if "$grok" whoami >/dev/null 2>&1; then
    return 0
  fi
  # 不主动烧额度做真实推理探测，仅看凭证文件 / 环境变量
  is_logged_in
}

cmd_login() {
  load_optional_env
  export_defaults

  local grok=""
  if ! grok="$(resolve_grok 2>/dev/null)"; then
    err "未找到 grok CLI。"
    err "请先执行：$0 install-cli"
    exit 1
  fi
  export GROK_CLI_COMMAND="$grok"
  log "CLI：$grok"

  if is_logged_in; then
    local authf=""
    authf="$(auth_file_present 2>/dev/null || true)"
    if [[ -n "${XAI_API_KEY:-}" ]]; then
      log "已检测到环境变量 XAI_API_KEY（无浏览器环境可用）"
    fi
    if [[ -n "$authf" ]]; then
      log "已检测到登录凭证：$authf"
    fi
    if probe_cli_auth "$grok"; then
      log "登录状态：OK"
      return 0
    fi
    log "凭证文件/环境变量存在，但 CLI 状态命令未确认；如请求失败请重新登录。"
    return 0
  fi

  log "未检测到登录凭证。"
  echo
  # 无 DISPLAY / Wayland 基本就是无 GUI 服务器
  local headless=0
  if [[ -z "${DISPLAY:-}" && -z "${WAYLAND_DISPLAY:-}" ]]; then
    headless=1
  fi

  if ((headless)); then
    echo "检测到无 GUI（无 DISPLAY）。"
    echo "想「真正登录」订阅账号时：在有浏览器的电脑 grok login，再把 auth.json 拷过来。"
    echo
  fi

  echo "可选登录方式："
  echo "  1) ★ 订阅登录：本机浏览器登录后，把凭证拷到服务器（你想登录时用这个）"
  echo "  2) 在本服务器跑 grok login（无 GUI 会尽量把登录 URL 打到终端）"
  echo "  3) API Key（按量计费兜底，不是订阅 OAuth）"
  echo "  q) 退出"
  echo

  # 非交互环境：只提示
  if [[ ! -t 0 ]] || [[ ! -t 1 ]]; then
    err "当前为非交互终端。订阅登录请："
    err "  有浏览器的电脑：grok login"
    err "  scp ~/.grok/auth.json 到服务器 ~/.grok/auth.json"
    err "  再执行：$0 login"
    exit 1
  fi

  local default_choice="1"
  echo -n "选择 [1=拷贝凭证登录 / 2=本机 grok login / 3=API Key / q=退出]（默认 ${default_choice}）> "
  local choice
  read -r choice
  choice="${choice:-$default_choice}"

  case "$choice" in
    1) login_copy_auth_guide ;;
    2) login_run_grok_login "$grok" "$headless" ;;
    3) login_paste_api_key ;;
    q|Q)
      log "已取消"
      exit 0
      ;;
    *)
      die "无效选择：$choice"
      ;;
  esac

  if is_logged_in; then
    log "登录状态：OK"
    if [[ -n "${XAI_API_KEY:-}" ]]; then
      log "方式：XAI_API_KEY"
    fi
    if authf="$(auth_file_present 2>/dev/null)"; then
      log "凭证文件：$authf"
    fi
    log "可手动验证：\"$grok\" --no-auto-update -p \"Say ok.\" --output-format plain"
  else
    err "仍未检测到凭证（~/.grok/auth.json 或 XAI_API_KEY）。"
    err "订阅登录步骤："
    err "  1) 有浏览器的电脑执行：grok login"
    err "  2) scp ~/.grok/auth.json 当前用户@服务器:~/.grok/auth.json"
    err "  3) 服务器：chmod 600 ~/.grok/auth.json && $0 login"
    exit 1
  fi
}

# 订阅登录：本机 OAuth 后把 auth.json 拷到服务器
login_copy_auth_guide() {
  local host_hint user_hint
  host_hint="$(hostname -f 2>/dev/null || hostname 2>/dev/null || echo '你的服务器')"
  user_hint="$(id -un 2>/dev/null || echo root)"

  echo
  echo "========== 订阅账号登录（无 GUI 服务器）=========="
  echo
  echo "原理：grok login = 浏览器 OAuth，token 写在 ~/.grok/auth.json。"
  echo "      在有浏览器的电脑登录一次，把该文件拷到服务器 = 服务器已登录。"
  echo "      （使用 SuperGrok / X Premium+ 订阅额度，不是 API 按量计费）"
  echo
  echo "—— A. 在你自己的 Windows / Mac 上 ——"
  echo "  1. 安装 CLI（没有的话）："
  echo "       Windows PowerShell:  irm https://x.ai/cli/install.ps1 | iex"
  echo "       Mac / Linux:         curl -fsSL https://x.ai/cli/install.sh | bash"
  echo "  2. 登录（会打开浏览器，用 xAI / X 账号授权）："
  echo "       grok login"
  echo "  3. 确认文件存在："
  echo "       Windows:   dir %USERPROFILE%\\.grok\\auth.json"
  echo "       Mac/Linux: ls -la ~/.grok/auth.json"
  echo
  echo "—— B. 拷到本服务器（用户 ${user_hint}@${host_hint}）——"
  echo "  先在服务器准备目录："
  echo "    mkdir -p ~/.grok && chmod 700 ~/.grok"
  echo
  echo "  在你电脑上执行（把 服务器IP 换成真实 IP）："
  echo "    # Windows PowerShell 示例："
  echo "    scp \$env:USERPROFILE\\.grok\\auth.json ${user_hint}@服务器IP:~/.grok/auth.json"
  echo "    # 或拷整个目录："
  echo "    scp -r \$env:USERPROFILE\\.grok ${user_hint}@服务器IP:~/"
  echo
  echo "    # Mac / Linux 示例："
  echo "    scp ~/.grok/auth.json ${user_hint}@服务器IP:~/.grok/auth.json"
  echo
  echo "—— C. 服务器收尾 ——"
  echo "    chmod 600 ~/.grok/auth.json"
  echo "    ./run.sh login     # 应显示 Login OK"
  echo "    ./run.sh start"
  echo
  echo "拷贝完成后按回车检测；还没拷可先 q 退出。"
  echo -n "[回车=检测 / q=退出] > "
  local ans
  read -r ans
  if [[ "$ans" == [Qq]* ]]; then
    log "先去本机 grok login 并 scp 凭证，完成后再运行：$0 login"
    exit 0
  fi
  mkdir -p "${HOME}/.grok"
  chmod 700 "${HOME}/.grok" 2>/dev/null || true
  if [[ -f "${HOME}/.grok/auth.json" ]]; then
    chmod 600 "${HOME}/.grok/auth.json" 2>/dev/null || true
    log "已找到 ${HOME}/.grok/auth.json"
  else
    # 有的版本可能用 credentials.json
    if authf="$(auth_file_present 2>/dev/null)"; then
      log "已找到凭证：$authf"
    else
      err "仍未找到 ${HOME}/.grok/auth.json"
      err "请确认 scp 的目标用户与当前用户一致（现在是 $(id -un)，HOME=$HOME）。"
      err "在服务器执行：ls -la ~/.grok/"
    fi
  fi
}

# 本机 grok login；无 GUI 时用 BROWSER 脚本把 URL 打到终端
login_run_grok_login() {
  local grok="$1"
  local headless="${2:-0}"
  local browser_helper=""

  echo
  if [[ "$headless" == "1" ]]; then
    echo "无 GUI：尽量把登录链接打印到终端（不弹窗）。"
    echo "出现 https://... 后，用手机/电脑浏览器打开并授权，再回到 SSH 等待。"
    echo
    echo "若授权后跳转到 http://127.0.0.1:端口 且服务器一直失败，"
    echo "说明回调只能在「发起登录的那台机器」完成 → 请改用选项 1（本机登录后 scp）。"
    echo
    echo -n "开始 grok login？[Y/n] > "
    local cont
    read -r cont
    if [[ "$cont" == [Nn]* ]]; then
      log "已取消。推荐：$0 login 选 1"
      exit 0
    fi

    browser_helper="$(mktemp "${TMPDIR:-/tmp}/grok-browser.XXXXXX")"
    cat > "$browser_helper" <<'EOS'
#!/bin/sh
echo ""
echo "========== 请用浏览器打开下面的登录链接 =========="
echo "$*"
echo "=================================================="
echo "（授权完成后回到本 SSH 窗口等待，不要关这个进程）"
echo ""
mkdir -p "${HOME}/.grok" 2>/dev/null || true
printf '%s\n' "$*" >> "${HOME}/.grok/last-login-url.txt" 2>/dev/null || true
exit 0
EOS
    chmod +x "$browser_helper"
    mkdir -p "${HOME}/.grok"
    export BROWSER="$browser_helper"
    export GROK_BROWSER="$browser_helper"
  fi

  log "启动：$grok login"
  set +e
  if "$grok" login; then
    :
  elif "$grok" auth login; then
    :
  else
    log "无 login 子命令，尝试启动交互会话（成功后 /quit 或 Ctrl-C）..."
    "$grok" || true
  fi
  set -e

  if [[ -n "$browser_helper" && -f "$browser_helper" ]]; then
    rm -f "$browser_helper"
  fi
}

login_paste_api_key() {
  echo
  echo "API Key 是按量计费路径，与「订阅 OAuth 登录」不是同一套。"
  echo "  ① 浏览器打开：https://console.x.ai/"
  echo "  ② 创建 / 复制 API Key（形如 xai-...）"
  echo "  ③ 粘贴到下方（不回显）"
  echo
  echo -n "请粘贴 XAI_API_KEY: "
  local key
  if [[ -t 0 ]]; then
    stty -echo 2>/dev/null || true
    read -r key
    stty echo 2>/dev/null || true
    echo
  else
    read -r key
  fi
  key="$(printf '%s' "$key" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/^["'\'']//' -e 's/["'\'']$//')"
  if [[ -z "$key" ]]; then
    die "API Key 为空"
  fi
  if [[ "$key" != xai-* && "$key" != xai_* ]]; then
    log "提示：密钥通常以 xai- 开头；若你确认无误可忽略。"
  fi
  local env_snippet="${ROOT}/.env.xai"
  umask 077
  printf 'export XAI_API_KEY=%q\n' "$key" > "$env_snippet"
  chmod 600 "$env_snippet"
  # shellcheck disable=SC1090
  source "$env_snippet"
  log "已写入 $env_snippet（权限 600）。./run.sh start 会自动加载。"
}

# ---------------------------------------------------------------------------
# expose — 对外开放 / 收回本机监听（写入 .env，start 时自动加载）
# ---------------------------------------------------------------------------

# env_file_set KEY VALUE — 更新 .env 中的 KEY（不存在则追加）
env_file_set() {
  local key="$1" val="$2"
  touch "$ENV_FILE"
  local tmp="${ENV_FILE}.tmp"
  grep -v "^${key}=" "$ENV_FILE" > "$tmp" 2>/dev/null || true
  printf '%s=%q\n' "$key" "$val" >> "$tmp"
  mv "$tmp" "$ENV_FILE"
  chmod 600 "$ENV_FILE" 2>/dev/null || true
}

gen_api_key() {
  if need_cmd openssl; then
    printf 'gk-%s' "$(openssl rand -hex 16)"
  else
    "$PYTHON" -c 'import secrets; print("gk-" + secrets.token_hex(16), end="")'
  fi
}

expose_show() {
  load_optional_env
  local host="${GROK_CLI_HOST:-127.0.0.1}"
  echo "---- 对外开放状态 ----"
  echo "监听地址 : $host"
  if [[ "$host" == "127.0.0.1" || "$host" == "localhost" || "$host" == "::1" ]]; then
    echo "范围     : 仅本机（外部/容器不可访问）"
  else
    echo "范围     : 对外开放（0.0.0.0 = 本机所有网卡，含 Docker 网桥/公网网卡）"
  fi
  if [[ -n "${GROK_CLI_API_KEY:-}" ]]; then
    echo "网关密钥 : 已设置（调用需 Authorization: Bearer <key>）"
  else
    echo "网关密钥 : 未设置（任何能连上端口的人都可白嫖你的额度！）"
  fi
  echo "----------------------"
}

# 若网关在运行则重启使配置生效
expose_apply() {
  if is_running; then
    log "网关正在运行，重启以应用新监听配置..."
    cmd_restart
  else
    log "网关未运行；下次 ./run.sh start 时生效"
  fi
}

cmd_expose() {
  local sub="${1:-}"
  shift || true
  case "$sub" in
    on|open)
      load_optional_env
      env_file_set GROK_CLI_HOST "0.0.0.0"
      export GROK_CLI_HOST="0.0.0.0"
      local key="${1:-${GROK_CLI_API_KEY:-}}"
      if [[ -z "$key" ]]; then
        key="$(gen_api_key)"
        log "未指定密钥，已随机生成"
      elif ((${#key} < 8)); then
        err "密钥太短（${#key} 位），已替换为随机强密钥"
        key="$(gen_api_key)"
      fi
      env_file_set GROK_CLI_API_KEY "$key"
      export GROK_CLI_API_KEY="$key"
      log "已写入 $ENV_FILE：GROK_CLI_HOST=0.0.0.0"
      log "网关密钥 GROK_CLI_API_KEY=${key}"
      log "调用方请求头：Authorization: Bearer ${key}"
      log "（HeySure 模型预设里 API Key 填这个值）"
      err "安全提醒：0.0.0.0 含公网网卡。若只想给本机 Docker 容器用，"
      err "请在云安全组/防火墙里保持 ${GROK_CLI_PORT:-8100} 端口对公网关闭。"
      expose_apply
      ;;
    off|local|close)
      load_optional_env
      env_file_set GROK_CLI_HOST "127.0.0.1"
      export GROK_CLI_HOST="127.0.0.1"
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
        err "非交互环境请用：$0 expose on [密钥] / off / show"
        exit 1
      fi
      echo -n "请选择 > "
      local c
      read -r c || return 0
      case "$c" in
        1) cmd_expose on ;;
        2)
          local k
          echo -n "输入网关密钥（≥8 位）> "
          read -r k
          cmd_expose on "$k"
          ;;
        3) cmd_expose off ;;
        0|"") return 0 ;;
        *) err "无效选项：$c" ;;
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
    保持 8100 端口对公网关闭（容器经宿主机网桥访问，不走公网）。
EOF
      exit 1
      ;;
  esac
}

load_optional_env() {
  # 代理配置（.env.proxy）优先于会话里未设置的项
  if [[ -f "$PROXY_ENV_FILE" ]]; then
    # shellcheck disable=SC1090
    source "$PROXY_ENV_FILE"
  fi
  # 若用户通过 login 选项 2 写入了 .env.xai，启动时自动加载
  if [[ -f "${ROOT}/.env.xai" ]]; then
    # shellcheck disable=SC1091
    source "${ROOT}/.env.xai"
  fi
  # 通用 .env（KEY=VALUE，可选）
  if [[ -f "${ROOT}/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "${ROOT}/.env"
    set +a
  fi
  apply_proxy_env
}

# ---------------------------------------------------------------------------
# start / stop / restart / status / logs / fg
# ---------------------------------------------------------------------------

cmd_start() {
  load_optional_env
  export_defaults
  ensure_runtime

  if is_running; then
    log "已在运行 (pid $(pid_of)) http://${GROK_CLI_HOST}:${GROK_CLI_PORT}"
    return 0
  fi

  need_cmd "$PYTHON" || die "未找到 $PYTHON，请先：$0 deps"
  [[ -f "${ROOT}/server.py" ]] || die "缺少 server.py"

  if ! resolve_grok >/dev/null 2>&1; then
    err "未找到 grok CLI。建议先：$0 install-cli && $0 login"
    err "仍将启动网关；请求到达时会因 CLI 缺失而失败。"
  else
    export GROK_CLI_COMMAND="$(resolve_grok)"
  fi

  if ! is_logged_in; then
    err "警告：未检测到 grok 登录凭证（~/.grok/auth.json 或 XAI_API_KEY）。"
    err "推理请求可能失败。可执行：$0 login"
  fi

  log "启动网关 → http://${GROK_CLI_HOST}:${GROK_CLI_PORT}/v1/chat/completions"
  log "CLI=${GROK_CLI_COMMAND:-<未设置>}  MODELS=${GROK_CLI_MODELS}"
  log "Proxy=$(proxy_display)"
  log "日志：$LOG_FILE"

  # -u：stdout 重定向到文件时禁用块缓冲，否则日志长期看似为空
  nohup "$PYTHON" -u server.py \
    >>"$LOG_FILE" 2>&1 &
  local pid=$!
  echo "$pid" > "$PID_FILE"

  # 短暂等待确认进程存活
  sleep 0.5
  if kill -0 "$pid" 2>/dev/null; then
    log "已启动 pid=$pid"
    log "健康检查：curl -sS http://${GROK_CLI_HOST}:${GROK_CLI_PORT}/"
  else
    rm -f "$PID_FILE"
    die "进程启动后立即退出，请查看日志：tail -n 50 $LOG_FILE"
  fi
}

cmd_stop() {
  if ! is_running; then
    log "未在运行"
    rm -f "$PID_FILE"
    return 0
  fi
  local pid
  pid="$(pid_of)"
  log "停止 pid=$pid ..."
  kill "$pid" 2>/dev/null || true

  local i
  for i in 1 2 3 4 5 6 7 8 9 10; do
    if ! kill -0 "$pid" 2>/dev/null; then
      break
    fi
    sleep 0.3
  done
  if kill -0 "$pid" 2>/dev/null; then
    log "强制 kill -9 $pid"
    kill -9 "$pid" 2>/dev/null || true
  fi
  rm -f "$PID_FILE"
  log "已停止"
}

cmd_restart() {
  cmd_stop
  cmd_start
}

cmd_status() {
  load_optional_env
  export_defaults
  echo "---- grok_cli status ----"
  echo "ROOT     : $ROOT"
  echo "HOST:PORT: ${GROK_CLI_HOST:-127.0.0.1}:${GROK_CLI_PORT:-8100}"
  if cmd="$(resolve_grok 2>/dev/null)"; then
    echo "CLI      : $cmd"
  else
    echo "CLI      : (未找到)"
  fi
  if is_logged_in; then
    local authf
    authf="$(auth_file_present 2>/dev/null || true)"
    if [[ -n "${XAI_API_KEY:-}" ]]; then
      echo "Login    : OK (XAI_API_KEY)"
    elif [[ -n "$authf" ]]; then
      echo "Login    : OK ($authf)"
    else
      echo "Login    : OK"
    fi
  else
    echo "Login    : 未登录"
  fi
  echo "Proxy    : $(proxy_display)"
  # 健康检查地址：监听 0.0.0.0 时本机探测用 127.0.0.1
  local hh="${GROK_CLI_HOST:-127.0.0.1}"
  if [[ "$hh" == "0.0.0.0" || "$hh" == "::" ]]; then
    hh="127.0.0.1"
  fi
  local url="http://${hh}:${GROK_CLI_PORT:-8100}/"
  if is_running; then
    echo "Gateway  : running (pid $(pid_of))"
    if need_cmd curl; then
      # 健康检查直连本机，避免被 http_proxy 劫持
      if out="$(curl -fsS --max-time 2 --noproxy '*' "$url" 2>/dev/null)"; then
        echo "Health   : OK  $out"
      else
        echo "Health   : 进程在但 HTTP 无响应（检查 host/port/防火墙）"
      fi
    fi
  else
    echo "Gateway  : stopped"
    # pid 文件丢失/不匹配，但端口上仍有服务在响应的情况
    if need_cmd curl; then
      if out="$(curl -fsS --max-time 2 --noproxy '*' "$url" 2>/dev/null)"; then
        echo "注意     : 端口 ${GROK_CLI_PORT:-8100} 仍有网关响应，但不是本脚本记录的进程"
        echo "           $out"
        echo "           可能：pid 文件丢失 / 在别的目录启动过。可 pkill -f 'server.py' 后重新 start"
      fi
    fi
  fi
  echo "Log      : $LOG_FILE"
  echo "-------------------------"
}

cmd_logs() {
  ensure_runtime
  if [[ ! -f "$LOG_FILE" ]]; then
    log "暂无日志文件：$LOG_FILE"
    return 0
  fi
  if [[ "${1:-}" == "-f" ]] || [[ "${1:-}" == "--follow" ]]; then
    tail -n 50 -f "$LOG_FILE"
  else
    tail -n 80 "$LOG_FILE"
  fi
}

cmd_fg() {
  load_optional_env
  export_defaults
  ensure_runtime
  need_cmd "$PYTHON" || die "未找到 $PYTHON，请先：$0 deps"
  if cmd="$(resolve_grok 2>/dev/null)"; then
    export GROK_CLI_COMMAND="$cmd"
  fi
  log "前台启动（Ctrl-C 退出）CLI=${GROK_CLI_COMMAND:-<未设置>}"
  exec "$PYTHON" -u server.py
}

# ---------------------------------------------------------------------------
# 交互菜单
# ---------------------------------------------------------------------------

menu() {
  while true; do
    load_optional_env 2>/dev/null || true
    echo
    echo "========== grok_cli 管理 =========="
    echo "  1) 安装系统依赖 (deps)"
    echo "  2) 安装 / 更新 grok CLI (install-cli)"
    echo "  3) 检查 / 完成登录 (login)"
    echo "  4) 配置系统代理 (proxy)   当前: $(proxy_display)"
    echo "  e) 对外开放/收回 (expose) 当前监听: ${GROK_CLI_HOST:-127.0.0.1}"
    echo "  5) 启动网关 (start)"
    echo "  6) 停止网关 (stop)"
    echo "  7) 重启网关 (restart)"
    echo "  8) 查看状态 (status)"
    echo "  9) 查看日志 (logs)"
    echo "  a) 前台启动调试 (fg)"
    echo "  0) 退出"
    echo "==================================="
    echo -n "请选择 > "
    local c
    read -r c || exit 0
    case "$c" in
      1) cmd_deps ;;
      2) cmd_install_cli ;;
      3) cmd_login ;;
      4) cmd_proxy ;;
      e|E) cmd_expose ;;
      5) cmd_start ;;
      6) cmd_stop ;;
      7) cmd_restart ;;
      8) cmd_status ;;
      9) cmd_logs ;;
      a|A|10) cmd_fg ;;
      0|q|Q) exit 0 ;;
      *) err "无效选项：$c" ;;
    esac
  done
}

usage() {
  cat <<'EOF'
用法: ./run.sh [命令]

命令:
  deps          安装系统依赖（python3 / curl）
  install-cli   安装或更新官方 grok CLI
  login         检查登录；未登录则引导 login 或填写 XAI_API_KEY
  proxy         配置系统 HTTP/HTTPS/SOCKS 代理（网址 + 端口）
    proxy set --host HOST --port PORT [--scheme http|socks5]
    proxy set --url http://HOST:PORT
    proxy show | test | clear
  expose        对外开放（0.0.0.0 + 强制网关密钥）/ 收回本机监听
    expose on [密钥] | off | show
  start         后台启动 OpenAI 兼容网关
  stop          停止网关
  restart       重启网关
  status        查看 CLI / 登录 / 代理 / 进程状态
  logs [-f]     查看日志（-f 跟踪）
  fg            前台启动（调试）
  help          显示帮助

无参数时进入交互菜单。

服务器部署示例:
  chmod +x run.sh
  ./run.sh deps
  # 需要翻墙/公司代理时先配：
  ./run.sh proxy set --host 127.0.0.1 --port 7890
  ./run.sh proxy test
  ./run.sh install-cli
  ./run.sh login
  # 若需 Docker 容器/局域网/公网访问网关（写入 .env，重启后仍生效）：
  #   ./run.sh expose on
  ./run.sh start
  ./run.sh status
EOF
}

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

main() {
  local cmd="${1:-}"
  case "$cmd" in
    "")           menu ;;
    deps|install-deps) shift || true; cmd_deps "$@" ;;
    install-cli|install_cli|cli) shift || true; cmd_install_cli "$@" ;;
    login|auth)   shift || true; cmd_login "$@" ;;
    proxy|proxies) shift || true; cmd_proxy "$@" ;;
    expose|open)  shift || true; cmd_expose "$@" ;;
    start)        shift || true; cmd_start "$@" ;;
    stop)         shift || true; cmd_stop "$@" ;;
    restart)      shift || true; cmd_restart "$@" ;;
    status)       shift || true; cmd_status "$@" ;;
    logs|log)     shift || true; cmd_logs "$@" ;;
    fg|foreground) shift || true; cmd_fg "$@" ;;
    help|-h|--help) usage ;;
    *)
      err "未知命令：$cmd"
      usage
      exit 1
      ;;
  esac
}

main "$@"
