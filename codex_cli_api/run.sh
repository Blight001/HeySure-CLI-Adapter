#!/usr/bin/env bash
# codex_cli 服务器管理脚本（Linux）
#
# 用法：
#   chmod +x run.sh
#   ./run.sh                 # 交互菜单
#   ./run.sh deps            # 安装系统依赖（python3 / curl 等）
#   ./run.sh install-cli     # 安装 / 更新 Codex CLI
#   ./run.sh login           # 检查登录；未登录则引导登录
#   ./run.sh expose on|off   # 对外开放（0.0.0.0 + 网关密钥）/ 收回本机
#   ./run.sh autostart on|off|status  # systemd 开机自启
#   ./run.sh start|stop|restart|status|logs
#   ./run.sh fg              # 前台启动（调试用）
#
# 环境变量（可选，与 server.py 一致）：
#   CODEX_CLI_COMMAND  CLI 路径（默认自动探测 ~/.codex/bin/codex 或 PATH 中的 codex）
#   CODEX_CLI_HOST     默认 127.0.0.1；对外暴露可设 0.0.0.0
#   CODEX_CLI_PORT     默认 8120
#   CODEX_CLI_TIMEOUT  默认 900
#   CODEX_CLI_API_KEY  网关鉴权（可选）
#   CODEX_CLI_MODELS   可选人工覆盖；默认由 codex debug models 动态发现
#   OPENAI_API_KEY     可选；仅供 ./run.sh login 的 API Key 登录方式读取
#   PYTHON            python 解释器，默认 python3
#   代理（也可 ./run.sh proxy 交互配置，写入 .env.proxy）：
#   CODEX_CLI_PROXY_HOST / CODEX_CLI_PROXY_PORT / CODEX_CLI_PROXY_SCHEME
#   CODEX_CLI_PROXY_URL  完整代理地址，如 http://127.0.0.1:7890
#   CODEX_CLI_PROXY_USER / CODEX_CLI_PROXY_PASS  可选认证

set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

RUNTIME_DIR="${ROOT}/runtime"
PID_FILE="${RUNTIME_DIR}/gateway.pid"
LOG_FILE="${RUNTIME_DIR}/gateway.log"
PROXY_ENV_FILE="${ROOT}/.env.proxy"
ENV_FILE="${ROOT}/.env"
PYTHON="${PYTHON:-python3}"
SYSTEMD_UNIT_NAME="codex-cli-gateway.service"
SYSTEMD_UNIT_FILE="/etc/systemd/system/${SYSTEMD_UNIT_NAME}"

# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

log()  { printf '[codex_cli] %s\n' "$*"; }
err()  { printf '[codex_cli] ERROR: %s\n' "$*" >&2; }
die()  { err "$*"; exit 1; }

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || return 1
}

ensure_runtime() {
  mkdir -p "$RUNTIME_DIR"
}

# 解析 Codex CLI 可执行路径（写入 CODEX_CLI_COMMAND 若未设置）
resolve_codex() {
  if [[ -n "${CODEX_CLI_COMMAND:-}" ]]; then
    # 允许 "path with spaces" 或完整路径
    local first
    first="$(printf '%s' "$CODEX_CLI_COMMAND" | awk '{print $1}')"
    if [[ -x "$first" ]] || need_cmd "$first" || [[ -f "$first" ]]; then
      printf '%s' "$CODEX_CLI_COMMAND"
      return 0
    fi
  fi

  local candidates=(
    "${HOME}/.codex/bin/codex"
    "${HOME}/.local/bin/codex"
    "/usr/local/bin/codex"
    "/usr/bin/codex"
  )
  local p
  for p in "${candidates[@]}"; do
    if [[ -x "$p" ]]; then
      printf '%s' "$p"
      return 0
    fi
  done
  if need_cmd codex; then
    command -v codex
    return 0
  fi
  return 1
}

export_defaults() {
  export CODEX_CLI_HOST="${CODEX_CLI_HOST:-127.0.0.1}"
  export CODEX_CLI_PORT="${CODEX_CLI_PORT:-8120}"
  export CODEX_CLI_TIMEOUT="${CODEX_CLI_TIMEOUT:-900}"
  export CODEX_CLI_MODELS="${CODEX_CLI_MODELS:-}"
  if [[ -z "${CODEX_CLI_COMMAND:-}" ]]; then
    if cmd="$(resolve_codex 2>/dev/null)"; then
      export CODEX_CLI_COMMAND="$cmd"
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
  local url="${CODEX_CLI_PROXY_URL:-}"
  if [[ -n "$url" ]]; then
    # 补全 scheme
    if [[ "$url" != *"://"* ]]; then
      url="http://${url}"
    fi
    printf '%s' "$url"
    return 0
  fi

  local host="${CODEX_CLI_PROXY_HOST:-}"
  local port="${CODEX_CLI_PROXY_PORT:-}"
  local scheme="${CODEX_CLI_PROXY_SCHEME:-http}"
  local user="${CODEX_CLI_PROXY_USER:-}"
  local pass="${CODEX_CLI_PROXY_PASS:-}"

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
    CODEX_CLI_PROXY_URL CODEX_CLI_PROXY_HOST CODEX_CLI_PROXY_PORT \
    CODEX_CLI_PROXY_SCHEME CODEX_CLI_PROXY_USER CODEX_CLI_PROXY_PASS \
    2>/dev/null || true
}

# 将配置导出为进程环境（curl / python / codex 子进程会继承）
apply_proxy_env() {
  local url=""
  if ! url="$(build_proxy_url 2>/dev/null)"; then
    return 0
  fi
  [[ -n "$url" ]] || return 0

  export CODEX_CLI_PROXY_URL="$url"
  export http_proxy="$url"
  export https_proxy="$url"
  export HTTP_PROXY="$url"
  export HTTPS_PROXY="$url"
  export all_proxy="$url"
  export ALL_PROXY="$url"

  local nop="localhost,127.0.0.1,::1,${CODEX_CLI_HOST:-127.0.0.1}"
  if [[ -n "${CODEX_CLI_NO_PROXY:-}" ]]; then
    nop="${CODEX_CLI_NO_PROXY}"
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
  local host="${CODEX_CLI_PROXY_HOST:-}"
  local port="${CODEX_CLI_PROXY_PORT:-}"
  local scheme="${CODEX_CLI_PROXY_SCHEME:-http}"
  local user="${CODEX_CLI_PROXY_USER:-}"
  local pass="${CODEX_CLI_PROXY_PASS:-}"
  local url="${CODEX_CLI_PROXY_URL:-}"
  local nop="${CODEX_CLI_NO_PROXY:-localhost,127.0.0.1,::1}"

  umask 077
  {
    echo "# codex_cli 代理配置 — 由 ./run.sh proxy 生成，start/install-cli 会自动加载"
    echo "# 也可手动 export 后启动；本文件优先于空环境"
    if [[ -n "$url" ]]; then
      printf 'export CODEX_CLI_PROXY_URL=%q\n' "$url"
    fi
    if [[ -n "$host" ]]; then
      printf 'export CODEX_CLI_PROXY_HOST=%q\n' "$host"
    fi
    if [[ -n "$port" ]]; then
      printf 'export CODEX_CLI_PROXY_PORT=%q\n' "$port"
    fi
    printf 'export CODEX_CLI_PROXY_SCHEME=%q\n' "$scheme"
    if [[ -n "$user" ]]; then
      printf 'export CODEX_CLI_PROXY_USER=%q\n' "$user"
    fi
    if [[ -n "$pass" ]]; then
      printf 'export CODEX_CLI_PROXY_PASS=%q\n' "$pass"
    fi
    printf 'export CODEX_CLI_NO_PROXY=%q\n' "$nop"
  } > "$PROXY_ENV_FILE"
  chmod 600 "$PROXY_ENV_FILE"

  # 立即应用并写回标准代理变量到文件，方便 source
  apply_proxy_env
  if url="$(build_proxy_url 2>/dev/null)"; then
    {
      echo ""
      echo "# 标准代理环境变量（供 curl / codex / python urllib 使用）"
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
  echo "HOST     : ${CODEX_CLI_PROXY_HOST:-(空)}"
  echo "PORT     : ${CODEX_CLI_PROXY_PORT:-(空)}"
  echo "SCHEME   : ${CODEX_CLI_PROXY_SCHEME:-http}"
  echo "USER     : ${CODEX_CLI_PROXY_USER:-(无)}"
  echo "URL      : $(proxy_display)"
  echo "NO_PROXY : ${NO_PROXY:-${CODEX_CLI_NO_PROXY:-localhost,127.0.0.1,::1}}"
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
      https://api.openai.com/ 2>/dev/null \
    || curl -fsS --max-time 15 -o /dev/null -w "HTTP %{http_code}  time %{time_total}s\n" \
      https://chatgpt.com/ 2>/dev/null \
    || curl -fsS --max-time 15 -o /dev/null -w "HTTP %{http_code}  time %{time_total}s\n" \
      https://www.google.com/generate_204 2>/dev/null; then
    log "代理连通性：OK"
  else
    err "代理探测失败。请检查网址/端口、协议(http/socks5)、账号密码与防火墙。"
    err "可手动：curl -x $(proxy_display) -I https://chatgpt.com/"
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
    export CODEX_CLI_PROXY_URL="$raw"
    # 尽量拆 host/port 便于展示
    local rest scheme
    scheme="${raw%%://*}"
    rest="${raw#*://}"
    export CODEX_CLI_PROXY_SCHEME="$scheme"
    # 去掉 userinfo
    if [[ "$rest" == *"@"* ]]; then
      local userinfo
      userinfo="${rest%%@*}"
      rest="${rest#*@}"
      if [[ "$userinfo" == *":"* ]]; then
        export CODEX_CLI_PROXY_USER="${userinfo%%:*}"
        export CODEX_CLI_PROXY_PASS="${userinfo#*:}"
      else
        export CODEX_CLI_PROXY_USER="$userinfo"
      fi
    fi
    # 去掉 path
    rest="${rest%%/*}"
    if [[ "$rest" == *"]:"* ]]; then
      # [ipv6]:port
      export CODEX_CLI_PROXY_HOST="${rest%:*}"
      export CODEX_CLI_PROXY_PORT="${rest##*:}"
    elif [[ "$rest" == *":"* ]]; then
      export CODEX_CLI_PROXY_HOST="${rest%:*}"
      export CODEX_CLI_PROXY_PORT="${rest##*:}"
    else
      export CODEX_CLI_PROXY_HOST="$rest"
    fi
    return 0
  fi

  # host:port
  if [[ "$raw" == *":"* ]]; then
    export CODEX_CLI_PROXY_HOST="${raw%:*}"
    export CODEX_CLI_PROXY_PORT="${raw##*:}"
    unset CODEX_CLI_PROXY_URL 2>/dev/null || true
    return 0
  fi

  # 仅 host
  export CODEX_CLI_PROXY_HOST="$raw"
  unset CODEX_CLI_PROXY_URL 2>/dev/null || true
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
          host="${CODEX_CLI_PROXY_HOST:-$host}"
          port="${CODEX_CLI_PROXY_PORT:-$port}"
          url="${CODEX_CLI_PROXY_URL:-$url}"
          scheme="${CODEX_CLI_PROXY_SCHEME:-$scheme}"
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
    export CODEX_CLI_PROXY_HOST="$host"
    export CODEX_CLI_PROXY_PORT="$port"
    export CODEX_CLI_PROXY_SCHEME="${scheme:-http}"
    unset CODEX_CLI_PROXY_URL 2>/dev/null || true
  fi
  if [[ -n "$user" ]]; then
    export CODEX_CLI_PROXY_USER="$user"
  fi
  if [[ -n "$pass" ]]; then
    export CODEX_CLI_PROXY_PASS="$pass"
  fi
  if [[ -n "$nop" ]]; then
    export CODEX_CLI_NO_PROXY="$nop"
  fi
  # 校验端口
  port="${CODEX_CLI_PROXY_PORT:-}"
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
      export CODEX_CLI_PROXY_HOST="$host"
      export CODEX_CLI_PROXY_PORT="$port"
      export CODEX_CLI_PROXY_SCHEME="$scheme"
      [[ -n "$user" ]] && export CODEX_CLI_PROXY_USER="$user"
      [[ -n "${pass:-}" ]] && export CODEX_CLI_PROXY_PASS="$pass"
      unset CODEX_CLI_PROXY_URL 2>/dev/null || true
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

systemd_available() {
  need_cmd systemctl && [[ -d /run/systemd/system ]]
}

systemd_unit_installed() {
  [[ -f "$SYSTEMD_UNIT_FILE" ]]
}

systemd_unit_active() {
  systemd_unit_installed && systemctl is-active --quiet "$SYSTEMD_UNIT_NAME" 2>/dev/null
}

systemd_unit_enabled() {
  systemd_unit_installed && systemctl is-enabled --quiet "$SYSTEMD_UNIT_NAME" 2>/dev/null
}

as_root() {
  if [[ "$(id -u)" -eq 0 ]]; then
    "$@"
  elif need_cmd sudo; then
    sudo "$@"
  else
    die "该操作需要 root 权限；请安装 sudo，或用 root 执行：$0 autostart"
  fi
}

# ---------------------------------------------------------------------------
# deps — 安装系统依赖（网关本身无 pip 依赖）
# ---------------------------------------------------------------------------

cmd_deps() {
  log "检查 / 安装系统依赖（python3、curl、Node.js/npm、ca-certificates）..."

  local missing=()
  need_cmd "$PYTHON" || missing+=("python3")
  need_cmd curl || missing+=("curl")
  need_cmd npm || missing+=("nodejs/npm")

  if ((${#missing[@]} == 0)); then
    log "已满足：$(command -v "$PYTHON")、$(command -v curl)、$(command -v npm)"
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
      err "  sudo apt-get update && sudo apt-get install -y python3 curl ca-certificates nodejs npm"
    elif need_cmd dnf; then
      err "  sudo dnf install -y python3 curl ca-certificates nodejs npm"
    elif need_cmd yum; then
      err "  sudo yum install -y python3 curl ca-certificates nodejs npm"
    elif need_cmd apk; then
      err "  sudo apk add python3 curl ca-certificates nodejs npm"
    else
      err "  请手动安装：python3 curl ca-certificates nodejs npm"
    fi
    err "或：sudo $0 deps"
    exit 1
  fi

  if need_cmd apt-get; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -y
    apt-get install -y python3 curl ca-certificates nodejs npm
  elif need_cmd dnf; then
    dnf install -y python3 curl ca-certificates nodejs npm
  elif need_cmd yum; then
    yum install -y python3 curl ca-certificates nodejs npm
  elif need_cmd apk; then
    apk add --no-cache python3 curl ca-certificates nodejs npm
  else
    die "无法识别包管理器，请手动安装 python3、curl、Node.js/npm"
  fi

  need_cmd "$PYTHON" || die "安装后仍找不到 $PYTHON"
  need_cmd curl || die "安装后仍找不到 curl"
  need_cmd npm || die "安装后仍找不到 npm"
  log "系统依赖安装完成"
}

# ---------------------------------------------------------------------------
# install-cli — 安装 / 更新官方 Codex CLI
# ---------------------------------------------------------------------------

cmd_install_cli() {
  load_optional_env
  need_cmd npm || die "需要 Node.js/npm，请先执行：$0 deps"
  if build_proxy_url >/dev/null 2>&1; then
    log "安装将走代理：$(proxy_display)"
  fi
  log "通过 npm 安装 / 更新官方 Codex CLI（@openai/codex）..."
  npm install -g @openai/codex

  if cmd="$(resolve_codex 2>/dev/null)"; then
    export CODEX_CLI_COMMAND="$cmd"
    log "Codex CLI 已就绪：$cmd"
    "$cmd" --version 2>/dev/null || "$cmd" -v 2>/dev/null || true
  else
    err "npm 安装已执行，但未在 PATH 或常见路径找到 codex。"
    err "请确认安装输出，或手动设置：export CODEX_CLI_COMMAND=/path/to/codex"
    exit 1
  fi
}

# ---------------------------------------------------------------------------
# login — 检查登录；未登录则引导
# ---------------------------------------------------------------------------

auth_file_present() {
  local f
  for f in \
    "${HOME}/.codex/auth.json" \
    "${CODEX_HOME:-${HOME}/.codex}/auth.json"
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
  local codex=""
  if codex="$(resolve_codex 2>/dev/null)"; then
    "$codex" login status >/dev/null 2>&1 && return 0
  fi
  auth_file_present >/dev/null
}

# 轻量探测：headless 发一句极短 prompt；失败不致命
probe_cli_auth() {
  local codex="$1"
  "$codex" login status >/dev/null 2>&1
}

cmd_login() {
  load_optional_env
  export_defaults

  local codex=""
  if ! codex="$(resolve_codex 2>/dev/null)"; then
    err "未找到 Codex CLI。"
    err "请先执行：$0 install-cli"
    exit 1
  fi
  export CODEX_CLI_COMMAND="$codex"
  log "CLI：$codex"

  if probe_cli_auth "$codex"; then
    log "登录状态：OK"
    "$codex" login status || true
    return 0
  fi

  log "未检测到登录凭证。"
  echo
  local headless=0
  if [[ -z "${DISPLAY:-}" && -z "${WAYLAND_DISPLAY:-}" ]]; then
    headless=1
    echo "检测到当前是无 GUI / SSH 环境，推荐选择 1（device auth）。"
    echo
  fi
  echo "可选登录方式："
  echo "  1) ★ Device Auth（无 GUI 服务器推荐，会显示网址和一次性代码）"
  echo "  2) ChatGPT 浏览器 OAuth（仅 GUI 或已配置 SSH 端口转发）"
  echo "  3) 从本机拷贝 ~/.codex/auth.json"
  echo "  4) OpenAI API Key（按量计费）"
  echo "  q) 退出"
  echo

  # 非交互环境：只提示
  if [[ ! -t 0 ]] || [[ ! -t 1 ]]; then
    err "当前为非交互终端。请在 SSH 交互终端运行 $0 login，或直接运行 codex login --device-auth。"
    exit 1
  fi

  local default_choice="1"
  echo -n "选择 [1=device auth / 2=浏览器 OAuth / 3=拷贝凭证 / 4=API Key / q=退出]（默认 ${default_choice}）> "
  local choice
  read -r choice
  choice="${choice:-$default_choice}"

  case "$choice" in
    1) "$codex" login --device-auth ;;
    2) login_browser_oauth "$codex" "$headless" ;;
    3) login_copy_auth_guide ;;
    4) login_paste_api_key "$codex" ;;
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
    "$codex" login status || true
    if authf="$(auth_file_present 2>/dev/null)"; then
      log "凭证文件：$authf"
    fi
  else
    err "Codex 仍报告未登录，请重试 device auth，或检查当前用户的 ~/.codex/auth.json。"
    exit 1
  fi
}

login_browser_oauth() {
  local codex="$1"
  local headless="${2:-0}"
  if [[ "$headless" != "1" ]]; then
    "$codex" login
    return
  fi

  need_cmd curl || die "SSH 回传 OAuth 需要 curl，请先执行：$0 deps"
  echo
  echo "将启动服务器本机的 OAuth 回调监听。授权后，本机浏览器会停在"
  echo "http://localhost:1455/auth/callback?...（页面打不开是正常的）。"
  echo "请复制浏览器地址栏的完整回调 URL，再粘贴回本 SSH；脚本会从服务器本机转交。"
  echo "注意：回调 URL 含一次性授权码，不要发送给他人，也不要保存到 shell 历史。"
  echo

  local login_log login_pid callback_url i
  login_log="$(mktemp "${TMPDIR:-/tmp}/codex-login.XXXXXX")"
  # 后台 Codex 不读取 SSH stdin，避免与下方 callback 输入提示争抢终端。
  "$codex" login </dev/null > >(tee "$login_log") 2>&1 &
  login_pid=$!
  trap 'kill "$login_pid" 2>/dev/null || true; rm -f "$login_log"' INT TERM

  # 等 Codex 打印授权地址再显示输入提示，避免后台输出把提示冲乱。
  for i in $(seq 1 100); do
    if grep -q 'oauth/authorize' "$login_log" 2>/dev/null; then
      break
    fi
    if ! kill -0 "$login_pid" 2>/dev/null; then
      if wait "$login_pid"; then :; else
        local early_rc=$?
        rm -f "$login_log"
        trap - INT TERM
        return "$early_rc"
      fi
      rm -f "$login_log"
      trap - INT TERM
      return 0
    fi
    sleep 0.1
  done

  echo
  echo -n "粘贴完整 callback URL（输入 q 取消）> "
  read -r callback_url
  if [[ "$callback_url" == "q" || "$callback_url" == "Q" ]]; then
    kill "$login_pid" 2>/dev/null || true
    wait "$login_pid" 2>/dev/null || true
    rm -f "$login_log"
    trap - INT TERM
    log "已取消浏览器 OAuth"
    return 1
  fi

  # 仅允许投递给 Codex 固定的本机回调端点，禁止把该输入变成任意 URL 请求。
  case "$callback_url" in
    'http://localhost:1455/auth/callback?'*|'http://127.0.0.1:1455/auth/callback?'*) ;;
    *)
      err "回调地址不合法；必须以 http://localhost:1455/auth/callback? 开头。"
      kill "$login_pid" 2>/dev/null || true
      wait "$login_pid" 2>/dev/null || true
      rm -f "$login_log"
      trap - INT TERM
      return 1
      ;;
  esac
  if [[ "$callback_url" != *'code='* || "$callback_url" != *'state='* ]]; then
    err "回调地址缺少 code 或 state 参数。"
    kill "$login_pid" 2>/dev/null || true
    wait "$login_pid" 2>/dev/null || true
    rm -f "$login_log"
    trap - INT TERM
    return 1
  fi

  if ! curl -fsS --noproxy '*' --max-time 15 "$callback_url" >/dev/null; then
    err "无法把回调交给服务器 localhost:1455；Codex 登录监听可能已经退出。"
    kill "$login_pid" 2>/dev/null || true
    wait "$login_pid" 2>/dev/null || true
    rm -f "$login_log"
    trap - INT TERM
    return 1
  fi
  log "回调已转交给 Codex，等待登录完成..."
  local login_rc=0
  wait "$login_pid" || login_rc=$?
  rm -f "$login_log"
  trap - INT TERM
  return "$login_rc"
}

# 订阅登录：本机 OAuth 后把 auth.json 拷到服务器
login_copy_auth_guide() {
  local host_hint user_hint
  host_hint="$(hostname -f 2>/dev/null || hostname 2>/dev/null || echo '你的服务器')"
  user_hint="$(id -un 2>/dev/null || echo root)"

  echo
  echo "========== 订阅账号登录（无 GUI 服务器）=========="
  echo
  echo "原理：Codex 可把认证缓存写在 ~/.codex/auth.json。"
  echo "      在有浏览器的电脑登录一次，把该文件拷到服务器 = 服务器已登录。"
  echo "      凭证非常敏感，只能在你自己的受信任机器之间复制。"
  echo
  echo "—— A. 在你自己的 Windows / Mac 上 ——"
  echo "  1. 安装 CLI：npm install -g @openai/codex"
  echo "  2. 登录（会打开浏览器，用 ChatGPT 账号授权）："
  echo "       codex login"
  echo "  3. 确认文件存在："
  echo "       Windows:   dir %USERPROFILE%\\.codex\\auth.json"
  echo "       Mac/Linux: ls -la ~/.codex/auth.json"
  echo
  echo "—— B. 拷到本服务器（用户 ${user_hint}@${host_hint}）——"
  echo "  先在服务器准备目录："
  echo "    mkdir -p ~/.codex && chmod 700 ~/.codex"
  echo
  echo "  在你电脑上执行（把 服务器IP 换成真实 IP）："
  echo "    # Windows PowerShell 示例："
  echo "    scp \$env:USERPROFILE\\.codex\\auth.json ${user_hint}@服务器IP:~/.codex/auth.json"
  echo "    # 或拷整个目录："
  echo "    scp -r \$env:USERPROFILE\\.codex ${user_hint}@服务器IP:~/"
  echo
  echo "    # Mac / Linux 示例："
  echo "    scp ~/.codex/auth.json ${user_hint}@服务器IP:~/.codex/auth.json"
  echo
  echo "—— C. 服务器收尾 ——"
  echo "    chmod 600 ~/.codex/auth.json"
  echo "    ./run.sh login     # 应显示 Login OK"
  echo "    ./run.sh start"
  echo
  echo "拷贝完成后按回车检测；还没拷可先 q 退出。"
  echo -n "[回车=检测 / q=退出] > "
  local ans
  read -r ans
  if [[ "$ans" == [Qq]* ]]; then
    log "先去本机 codex login 并 scp 凭证，完成后再运行：$0 login"
    exit 0
  fi
  mkdir -p "${HOME}/.codex"
  chmod 700 "${HOME}/.codex" 2>/dev/null || true
  if [[ -f "${HOME}/.codex/auth.json" ]]; then
    chmod 600 "${HOME}/.codex/auth.json" 2>/dev/null || true
    log "已找到 ${HOME}/.codex/auth.json"
  else
    # 有的版本可能用 credentials.json
    if authf="$(auth_file_present 2>/dev/null)"; then
      log "已找到凭证：$authf"
    else
      err "仍未找到 ${HOME}/.codex/auth.json"
      err "请确认 scp 的目标用户与当前用户一致（现在是 $(id -un)，HOME=$HOME）。"
      err "在服务器执行：ls -la ~/.codex/"
    fi
  fi
}

login_paste_api_key() {
  local codex="$1"
  echo
  echo "API Key 是按量计费路径，与「订阅 OAuth 登录」不是同一套。"
  echo "  ① 浏览器打开：https://platform.openai.com/api-keys"
  echo "  ② 创建 / 复制 OpenAI API Key"
  echo "  ③ 粘贴到下方（不回显）"
  echo
  echo -n "请粘贴 OPENAI_API_KEY: "
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
  printf '%s' "$key" | "$codex" login --with-api-key
  unset key
  log "API Key 已交给 Codex CLI 保存；不会写入本项目的 .env。"
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
    printf 'cxg-%s' "$(openssl rand -hex 16)"
  else
    "$PYTHON" -c 'import secrets; print("cxg-" + secrets.token_hex(16), end="")'
  fi
}

expose_show() {
  load_optional_env
  local host="${CODEX_CLI_HOST:-127.0.0.1}"
  echo "---- 对外开放状态 ----"
  echo "监听地址 : $host"
  if [[ "$host" == "127.0.0.1" || "$host" == "localhost" || "$host" == "::1" ]]; then
    echo "范围     : 仅本机（外部/容器不可访问）"
  else
    echo "范围     : 对外开放（0.0.0.0 = 本机所有网卡，含 Docker 网桥/公网网卡）"
  fi
  if [[ -n "${CODEX_CLI_API_KEY:-}" ]]; then
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
      env_file_set CODEX_CLI_HOST "0.0.0.0"
      export CODEX_CLI_HOST="0.0.0.0"
      local key="${1:-${CODEX_CLI_API_KEY:-}}"
      if [[ -z "$key" ]]; then
        key="$(gen_api_key)"
        log "未指定密钥，已随机生成"
      elif ((${#key} < 8)); then
        err "密钥太短（${#key} 位），已替换为随机强密钥"
        key="$(gen_api_key)"
      fi
      env_file_set CODEX_CLI_API_KEY "$key"
      export CODEX_CLI_API_KEY="$key"
      log "已写入 $ENV_FILE：CODEX_CLI_HOST=0.0.0.0"
      log "网关密钥 CODEX_CLI_API_KEY=${key}"
      log "调用方请求头：Authorization: Bearer ${key}"
      log "（HeySure 模型预设里 API Key 填这个值）"
      err "安全提醒：0.0.0.0 含公网网卡。若只想给本机 Docker 容器用，"
      err "请在云安全组/防火墙里保持 ${CODEX_CLI_PORT:-8120} 端口对公网关闭。"
      expose_apply
      ;;
    off|local|close)
      load_optional_env
      env_file_set CODEX_CLI_HOST "127.0.0.1"
      export CODEX_CLI_HOST="127.0.0.1"
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
    保持 8120 端口对公网关闭（容器经宿主机网桥访问，不走公网）。
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
# systemd 开机自启
# ---------------------------------------------------------------------------

autostart_status_text() {
  if ! systemd_unit_installed; then
    printf '未配置'
  elif systemd_unit_enabled; then
    printf '已启用'
  else
    printf '已禁用'
  fi
}

cmd_autostart_enable() {
  systemd_available || die "当前系统未使用 systemd，无法配置开机自启"

  local service_user service_group service_home bash_path tmp_file
  service_user="${SUDO_USER:-$(id -un)}"
  service_group="$(id -gn "$service_user" 2>/dev/null || id -gn)"
  service_home="$(getent passwd "$service_user" 2>/dev/null | cut -d: -f6 || true)"
  [[ -n "$service_home" ]] || service_home="${HOME}"
  bash_path="$(command -v bash)"
  tmp_file="$(mktemp)"

  # 使用最朴素、兼容旧版 systemd 的单元语法。Linux 部署目录和 HOME 通常
  # 不含空白；若包含则明确报错，避免生成一个 systemd 无法解析的服务。
  case "${ROOT}${service_home}${bash_path}" in
    *[[:space:]]*)
      rm -f "$tmp_file"
      die "systemd 自启暂不支持路径中包含空格或换行：$ROOT"
      ;;
  esac

  {
    printf '[Unit]\n'
    printf 'Description=Codex CLI OpenAI-compatible gateway\n'
    printf 'After=network-online.target\n'
    printf 'Wants=network-online.target\n\n'
    printf '[Service]\n'
    printf 'Type=simple\n'
    printf 'User=%s\n' "$service_user"
    printf 'Group=%s\n' "$service_group"
    printf 'Environment=HOME=%s\n' "$service_home"
    printf 'WorkingDirectory=%s\n' "$ROOT"
    printf 'ExecStart=%s %s/run.sh fg\n' "$bash_path" "$ROOT"
    printf 'Restart=on-failure\n'
    printf 'RestartSec=3\n'
    printf 'TimeoutStopSec=20\n\n'
    printf '[Install]\n'
    printf 'WantedBy=multi-user.target\n'
  } > "$tmp_file"

  # 避免原 nohup 实例与 systemd 实例同时占用端口。
  if is_running; then
    cmd_stop
  fi

  as_root install -m 0644 "$tmp_file" "$SYSTEMD_UNIT_FILE"
  rm -f "$tmp_file"
  as_root systemctl daemon-reload

  # enable 之前先验证，避免把无效服务挂进 multi-user.target。
  if need_cmd systemd-analyze; then
    if ! systemd-analyze verify "$SYSTEMD_UNIT_FILE"; then
      as_root systemctl disable "$SYSTEMD_UNIT_NAME" >/dev/null 2>&1 || true
      echo "----- $SYSTEMD_UNIT_FILE -----" >&2
      as_root sed -n '1,120p' "$SYSTEMD_UNIT_FILE" >&2 || true
      die "systemd 单元校验失败（上方输出包含具体配置行）"
    fi
  fi
  as_root systemctl enable "$SYSTEMD_UNIT_NAME"
  as_root systemctl restart "$SYSTEMD_UNIT_NAME"

  sleep 0.5
  if systemd_unit_active; then
    log "开机自启已启用，网关已由 systemd 启动"
    log "查看状态：$0 status"
  else
    err "服务未能正常启动，请查看：journalctl -u $SYSTEMD_UNIT_NAME -n 80"
    return 1
  fi
}

cmd_autostart_disable() {
  systemd_available || die "当前系统未使用 systemd"
  if ! systemd_unit_installed; then
    log "尚未配置开机自启"
    return 0
  fi
  as_root systemctl disable "$SYSTEMD_UNIT_NAME"
  log "开机自启已禁用；当前服务运行状态未改变，需要停止可执行：$0 stop"
}

cmd_autostart_status() {
  echo "Autostart: $(autostart_status_text)"
  if systemd_unit_installed; then
    if systemd_unit_active; then
      echo "Service  : active ($SYSTEMD_UNIT_NAME)"
    else
      echo "Service  : inactive ($SYSTEMD_UNIT_NAME)"
    fi
    echo "Unit file: $SYSTEMD_UNIT_FILE"
  fi
}

cmd_autostart() {
  local action="${1:-}"
  case "$action" in
    on|enable|enabled) cmd_autostart_enable ;;
    off|disable|disabled) cmd_autostart_disable ;;
    status|show) cmd_autostart_status ;;
    "")
      if [[ -t 0 && -t 1 ]]; then
        cmd_autostart_status
        echo "  1) 启用开机自启（并立即启动）"
        echo "  2) 禁用开机自启（不停止当前服务）"
        echo "  0) 返回"
        echo -n "请选择 > "
        local choice
        read -r choice || return 0
        case "$choice" in
          1) cmd_autostart_enable ;;
          2) cmd_autostart_disable ;;
          0|q|Q) return 0 ;;
          *) err "无效选项：$choice" ;;
        esac
      else
        cmd_autostart_status
      fi
      ;;
    *) die "用法：$0 autostart on|off|status" ;;
  esac
}

# ---------------------------------------------------------------------------
# start / stop / restart / status / logs / fg
# ---------------------------------------------------------------------------

cmd_start() {
  if systemd_unit_installed; then
    systemd_available || die "已安装 systemd 服务，但当前无法使用 systemctl"
    as_root systemctl start "$SYSTEMD_UNIT_NAME"
    log "网关已交给 systemd 启动"
    return 0
  fi
  load_optional_env
  export_defaults
  ensure_runtime

  if is_running; then
    log "已在运行 (pid $(pid_of)) http://${CODEX_CLI_HOST}:${CODEX_CLI_PORT}"
    return 0
  fi

  need_cmd "$PYTHON" || die "未找到 $PYTHON，请先：$0 deps"
  [[ -f "${ROOT}/server.py" ]] || die "缺少 server.py"

  if ! resolve_codex >/dev/null 2>&1; then
    err "未找到 Codex CLI。建议先：$0 install-cli && $0 login"
    err "仍将启动网关；请求到达时会因 CLI 缺失而失败。"
  else
    export CODEX_CLI_COMMAND="$(resolve_codex)"
  fi

  if ! is_logged_in; then
    err "警告：Codex CLI 当前未登录。"
    err "推理请求可能失败。可执行：$0 login"
  fi

  log "启动网关 → http://${CODEX_CLI_HOST}:${CODEX_CLI_PORT}/v1/chat/completions"
  log "CLI=${CODEX_CLI_COMMAND:-<未设置>}  MODELS=${CODEX_CLI_MODELS:-<由 Codex CLI 动态发现>}"
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
    log "健康检查：curl -sS http://${CODEX_CLI_HOST}:${CODEX_CLI_PORT}/"
  else
    rm -f "$PID_FILE"
    die "进程启动后立即退出，请查看日志：tail -n 50 $LOG_FILE"
  fi
}

cmd_stop() {
  if systemd_unit_active; then
    as_root systemctl stop "$SYSTEMD_UNIT_NAME"
    log "systemd 网关已停止"
    return 0
  fi
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
  if systemd_unit_installed; then
    systemd_available || die "已安装 systemd 服务，但当前无法使用 systemctl"
    as_root systemctl restart "$SYSTEMD_UNIT_NAME"
    log "systemd 网关已重启"
    return 0
  fi
  cmd_stop
  cmd_start
}

cmd_status() {
  load_optional_env
  export_defaults
  echo "---- codex_cli status ----"
  echo "ROOT     : $ROOT"
  echo "HOST:PORT: ${CODEX_CLI_HOST:-127.0.0.1}:${CODEX_CLI_PORT:-8120}"
  echo "Autostart: $(autostart_status_text)"
  if cmd="$(resolve_codex 2>/dev/null)"; then
    echo "CLI      : $cmd"
  else
    echo "CLI      : (未找到)"
  fi
  if is_logged_in; then
    local authf
    authf="$(auth_file_present 2>/dev/null || true)"
    if [[ -n "$authf" ]]; then
      echo "Login    : OK ($authf)"
    else
      echo "Login    : OK"
    fi
  else
    echo "Login    : 未登录"
  fi
  echo "Proxy    : $(proxy_display)"
  # 健康检查地址：监听 0.0.0.0 时本机探测用 127.0.0.1
  local hh="${CODEX_CLI_HOST:-127.0.0.1}"
  if [[ "$hh" == "0.0.0.0" || "$hh" == "::" ]]; then
    hh="127.0.0.1"
  fi
  local url="http://${hh}:${CODEX_CLI_PORT:-8120}/"
  local curl_auth=()
  if [[ -n "${CODEX_CLI_API_KEY:-}" ]]; then
    curl_auth=(-H "Authorization: Bearer ${CODEX_CLI_API_KEY}")
  fi
  if systemd_unit_active || is_running; then
    if systemd_unit_active; then
      echo "Gateway  : running (systemd: $SYSTEMD_UNIT_NAME)"
    else
      echo "Gateway  : running (pid $(pid_of))"
    fi
    if need_cmd curl; then
      # 健康检查直连本机，避免被 http_proxy 劫持
      if out="$(curl -fsS --max-time 2 --noproxy '*' "${curl_auth[@]}" "$url" 2>/dev/null)"; then
        echo "Health   : OK  $out"
      else
        echo "Health   : 进程在但 HTTP 无响应（检查 host/port/防火墙）"
      fi
    fi
  else
    echo "Gateway  : stopped"
    # pid 文件丢失/不匹配，但端口上仍有服务在响应的情况
    if need_cmd curl; then
      if out="$(curl -fsS --max-time 2 --noproxy '*' "${curl_auth[@]}" "$url" 2>/dev/null)"; then
        echo "注意     : 端口 ${CODEX_CLI_PORT:-8120} 仍有网关响应，但不是本脚本记录的进程"
        echo "           $out"
        echo "           可能：pid 文件丢失 / 在别的目录启动过。可 pkill -f 'server.py' 后重新 start"
      fi
    fi
  fi
  echo "Log      : $LOG_FILE"
  echo "-------------------------"
}

cmd_logs() {
  if systemd_unit_installed && need_cmd journalctl; then
    if [[ "${1:-}" == "-f" ]] || [[ "${1:-}" == "--follow" ]]; then
      journalctl -u "$SYSTEMD_UNIT_NAME" -n 50 -f
    else
      journalctl -u "$SYSTEMD_UNIT_NAME" -n 80 --no-pager
    fi
    return 0
  fi
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
  if cmd="$(resolve_codex 2>/dev/null)"; then
    export CODEX_CLI_COMMAND="$cmd"
  fi
  log "前台启动（Ctrl-C 退出）CLI=${CODEX_CLI_COMMAND:-<未设置>}"
  exec "$PYTHON" -u server.py
}

# ---------------------------------------------------------------------------
# 交互菜单
# ---------------------------------------------------------------------------

menu() {
  while true; do
    load_optional_env 2>/dev/null || true
    echo
    echo "========== codex_cli 管理 =========="
    echo "  1) 安装系统依赖 (deps)"
    echo "  2) 安装 / 更新 Codex CLI (install-cli)"
    echo "  3) 检查 / 完成登录 (login)"
    echo "  4) 配置系统代理 (proxy)   当前: $(proxy_display)"
    echo "  e) 对外开放/收回 (expose) 当前监听: ${CODEX_CLI_HOST:-127.0.0.1}"
    echo "  5) 启动网关 (start)"
    echo "  6) 停止网关 (stop)"
    echo "  7) 重启网关 (restart)"
    echo "  8) 查看状态 (status)"
    echo "  9) 查看日志 (logs)"
    echo "  s) 开机自启 (autostart) 当前: $(autostart_status_text)"
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
      s|S) cmd_autostart ;;
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
  deps          安装系统依赖（python3 / curl / Node.js / npm）
  install-cli   安装或更新官方 Codex CLI
  login         检查登录；支持 device auth、浏览器 OAuth、凭证复制或 API Key
  proxy         配置系统 HTTP/HTTPS/SOCKS 代理（网址 + 端口）
    proxy set --host HOST --port PORT [--scheme http|socks5]
    proxy set --url http://HOST:PORT
    proxy show | test | clear
  expose        对外开放（0.0.0.0 + 强制网关密钥）/ 收回本机监听
    expose on [密钥] | off | show
  autostart     管理 systemd 开机自启
    autostart on | off | status
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
  ./run.sh autostart on
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
    autostart|boot|service) shift || true; cmd_autostart "$@" ;;
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
