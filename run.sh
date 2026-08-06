#!/usr/bin/env sh

# HeySure CLI Adapter Linux installer and service manager.
# POSIX sh compatible: OpenCloudOS/CentOS/RHEL/Debian/Ubuntu.
set -u

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
VENV_DIR="$SCRIPT_DIR/.venv"
PYTHON_BIN="$VENV_DIR/bin/python"
RUNTIME_DIR="$SCRIPT_DIR/control_runtime"
PID_FILE="$RUNTIME_DIR/agent.pid"
LOG_FILE="$RUNTIME_DIR/agent.log"
SERVICE_NAME="heysure-cli-adapter.service"
CONTROL_HOST=${HEYSURE_CLI_CONTROL_HOST:-0.0.0.0}
CONTROL_PORT=${HEYSURE_CLI_CONTROL_PORT:-8130}

case "$CONTROL_HOST" in
    *[!A-Za-z0-9._:-]*)
        printf '%s\n' "错误: HEYSURE_CLI_CONTROL_HOST 包含无效字符: $CONTROL_HOST" >&2
        exit 1
        ;;
esac
case "$CONTROL_PORT" in
    ''|*[!0-9]*)
        printf '%s\n' "错误: HEYSURE_CLI_CONTROL_PORT 必须是数字: $CONTROL_PORT" >&2
        exit 1
        ;;
esac

if [ "$(id -u)" -eq 0 ]; then
    SERVICE_SCOPE="system"
    SERVICE_FILE="/etc/systemd/system/$SERVICE_NAME"
else
    SERVICE_SCOPE="user"
    SERVICE_FILE="$HOME/.config/systemd/user/$SERVICE_NAME"
fi

say() {
    printf '%s\n' "$*"
}

die() {
    say "错误: $*" >&2
    exit 1
}

run_as_root() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    elif command -v sudo >/dev/null 2>&1; then
        sudo "$@"
    else
        die "该操作需要 root 权限，请使用 sudo 重新运行"
    fi
}

systemctl_run() {
    if [ "$SERVICE_SCOPE" = "system" ]; then
        systemctl "$@"
    else
        systemctl --user "$@"
    fi
}

install_system_python() {
    say "正在安装 Python、pip 与虚拟环境支持..."
    if command -v dnf >/dev/null 2>&1; then
        run_as_root dnf install -y python3 python3-pip
    elif command -v yum >/dev/null 2>&1; then
        run_as_root yum install -y python3 python3-pip
    elif command -v apt-get >/dev/null 2>&1; then
        run_as_root apt-get update
        run_as_root apt-get install -y python3 python3-pip python3-venv
    elif command -v apk >/dev/null 2>&1; then
        run_as_root apk add python3 py3-pip
    else
        die "无法识别系统包管理器，请先手动安装 Python 3、pip 和 venv"
    fi
}

install_deps() {
    say "[1/3] 检查系统 Python"
    if ! command -v python3 >/dev/null 2>&1; then
        install_system_python
    fi

    say "[2/3] 创建项目虚拟环境: $VENV_DIR"
    if [ ! -x "$PYTHON_BIN" ]; then
        if ! python3 -m venv "$VENV_DIR" >/dev/null 2>&1; then
            install_system_python
            if ! python3 -m venv "$VENV_DIR"; then
                python3 -m pip install --user virtualenv || die "无法安装 virtualenv"
                python3 -m virtualenv "$VENV_DIR" || die "无法创建虚拟环境"
            fi
        fi
    fi
    if ! "$PYTHON_BIN" -m pip --version >/dev/null 2>&1; then
        "$PYTHON_BIN" -m ensurepip --upgrade || die "无法在虚拟环境中安装 pip"
    fi

    say "[3/3] 安装 / 更新 Python 依赖"
    "$PYTHON_BIN" -m pip install --upgrade pip
    "$PYTHON_BIN" -m pip install -r "$SCRIPT_DIR/requirements.txt" || die "依赖安装失败"
    "$PYTHON_BIN" -c "import socketio" || die "python-socketio 安装验证失败"
    say "依赖安装完成。"
}

ensure_runtime() {
    if [ ! -x "$PYTHON_BIN" ] || ! "$PYTHON_BIN" -c "import socketio" >/dev/null 2>&1; then
        say "检测到依赖尚未安装，开始自动安装。"
        install_deps
    fi
    mkdir -p "$RUNTIME_DIR"
}

pid_is_running() {
    [ -f "$PID_FILE" ] || return 1
    pid=$(cat "$PID_FILE" 2>/dev/null || true)
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null || return 1
    if [ -r "/proc/$pid/cmdline" ]; then
        tr '\000' ' ' <"/proc/$pid/cmdline" | grep -F "$SCRIPT_DIR/agent.py" >/dev/null 2>&1
    fi
}

service_is_installed() {
    command -v systemctl >/dev/null 2>&1 && [ -f "$SERVICE_FILE" ]
}

start_service() {
    ensure_runtime
    if service_is_installed; then
        say "正在通过 systemd 启动服务..."
        systemctl_run start "$SERVICE_NAME" || die "systemd 服务启动失败，请运行 ./run.sh logs 查看日志"
    else
        if pid_is_running; then
            say "服务已经运行，PID: $(cat "$PID_FILE")"
            return 0
        fi
        rm -f "$PID_FILE"
        say "正在后台启动服务..."
        (
            cd "$SCRIPT_DIR" || exit 1
            nohup "$PYTHON_BIN" "$SCRIPT_DIR/agent.py" >>"$LOG_FILE" 2>&1 &
            echo $! >"$PID_FILE"
        )
        sleep 1
        if ! pid_is_running; then
            rm -f "$PID_FILE"
            say "启动失败，最近日志：" >&2
            tail -n 40 "$LOG_FILE" 2>/dev/null || true
            exit 1
        fi
    fi
    say "服务已启动。管理页: http://$CONTROL_HOST:$CONTROL_PORT/"
}

stop_pid_service() {
    if ! pid_is_running; then
        rm -f "$PID_FILE"
        return 0
    fi
    pid=$(cat "$PID_FILE")
    say "正在停止进程 $pid..."
    kill "$pid" 2>/dev/null || true
    count=0
    while kill -0 "$pid" 2>/dev/null && [ "$count" -lt 10 ]; do
        sleep 1
        count=$((count + 1))
    done
    if kill -0 "$pid" 2>/dev/null; then
        say "进程未在 10 秒内退出，正在强制停止。"
        kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$PID_FILE"
}

stop_service() {
    if service_is_installed; then
        systemctl_run stop "$SERVICE_NAME" >/dev/null 2>&1 || true
    fi
    stop_pid_service
    say "服务已停止。"
}

restart_service() {
    if service_is_installed; then
        say "正在检查并重建 systemd 服务配置..."
        write_service_file
        systemctl_run reset-failed "$SERVICE_NAME" >/dev/null 2>&1 || true
        if ! systemctl_run restart "$SERVICE_NAME"; then
            show_service_diagnostics
            die "服务重启失败，诊断信息见上方"
        fi
        say "服务已重启。"
    else
        stop_pid_service
        start_service
    fi
}

show_status() {
    say "HeySure CLI Adapter 状态"
    say "项目目录: $SCRIPT_DIR"
    say "管理地址: http://$CONTROL_HOST:$CONTROL_PORT/"
    if service_is_installed; then
        enabled=$(systemctl_run is-enabled "$SERVICE_NAME" 2>/dev/null || true)
        active=$(systemctl_run is-active "$SERVICE_NAME" 2>/dev/null || true)
        say "systemd: $active；开机自启: $enabled"
        systemctl_run status "$SERVICE_NAME" --no-pager -l 2>/dev/null | tail -n 12 || true
    elif pid_is_running; then
        say "运行状态: 已运行（PID $(cat "$PID_FILE")）"
        say "开机自启: 未配置"
    else
        say "运行状态: 未运行"
        say "开机自启: 未配置"
    fi
}

show_logs() {
    lines=${1:-100}
    if service_is_installed; then
        if [ "$SERVICE_SCOPE" = "system" ]; then
            journalctl -u "$SERVICE_NAME" -n "$lines" --no-pager
        else
            journalctl --user -u "$SERVICE_NAME" -n "$lines" --no-pager
        fi
    elif [ -f "$LOG_FILE" ]; then
        tail -n "$lines" "$LOG_FILE"
    else
        say "暂无日志。"
    fi
}

follow_logs() {
    if service_is_installed; then
        if [ "$SERVICE_SCOPE" = "system" ]; then
            journalctl -u "$SERVICE_NAME" -n 100 -f
        else
            journalctl --user -u "$SERVICE_NAME" -n 100 -f
        fi
    else
        touch "$LOG_FILE"
        tail -n 100 -f "$LOG_FILE"
    fi
}

verify_service_file() {
    [ -f "$SERVICE_FILE" ] || return 1
    if ! command -v systemd-analyze >/dev/null 2>&1; then
        return 0
    fi
    verify_output=$(systemd-analyze verify "$SERVICE_FILE" 2>&1) || {
        say "systemd 单元文件校验失败：" >&2
        say "$verify_output" >&2
        return 1
    }
}

show_service_diagnostics() {
    say "" >&2
    say "===== systemd 单元文件 =====" >&2
    if [ -r "$SERVICE_FILE" ]; then
        sed -n '1,160p' "$SERVICE_FILE" >&2
    else
        say "无法读取 $SERVICE_FILE" >&2
    fi
    say "===== systemd-analyze verify =====" >&2
    if command -v systemd-analyze >/dev/null 2>&1; then
        systemd-analyze verify "$SERVICE_FILE" >&2 || true
    else
        say "当前系统没有 systemd-analyze" >&2
    fi
    say "===== systemctl status =====" >&2
    systemctl_run status "$SERVICE_NAME" --no-pager -l >&2 || true
}

write_service_file() {
    ensure_runtime
    command -v systemctl >/dev/null 2>&1 || die "当前系统未安装 systemd"
    service_dir=$(dirname "$SERVICE_FILE")
    if [ "$SERVICE_SCOPE" = "system" ]; then
        run_as_root mkdir -p "$service_dir"
        tmp_file="$RUNTIME_DIR/$SERVICE_NAME.tmp"
        cat >"$tmp_file" <<EOF
[Unit]
Description=HeySure Unified CLI Adapter
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$SCRIPT_DIR
Environment="PYTHONUNBUFFERED=1"
ExecStart="$PYTHON_BIN" "$SCRIPT_DIR/agent.py" --host "$CONTROL_HOST" --port "$CONTROL_PORT"
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
        run_as_root install -m 0644 "$tmp_file" "$SERVICE_FILE"
        rm -f "$tmp_file"
    else
        mkdir -p "$service_dir"
        cat >"$SERVICE_FILE" <<EOF
[Unit]
Description=HeySure Unified CLI Adapter
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$SCRIPT_DIR
Environment="PYTHONUNBUFFERED=1"
ExecStart="$PYTHON_BIN" "$SCRIPT_DIR/agent.py" --host "$CONTROL_HOST" --port "$CONTROL_PORT"
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
EOF
    fi
    if ! verify_service_file; then
        show_service_diagnostics
        die "生成的 systemd 单元文件无效"
    fi
    systemctl_run daemon-reload || die "systemd 配置重载失败"
}

autostart_on() {
    stop_pid_service
    write_service_file
    systemctl_run enable --now "$SERVICE_NAME" || die "开机自启启用失败"
    say "开机自启已启用，服务已经启动。"
}

autostart_off() {
    if service_is_installed; then
        systemctl_run disable --now "$SERVICE_NAME" || true
        say "开机自启已关闭，服务已经停止。"
    else
        say "尚未配置 systemd 开机自启。"
    fi
}

foreground() {
    ensure_runtime
    cd "$SCRIPT_DIR" || exit 1
    exec "$PYTHON_BIN" "$SCRIPT_DIR/agent.py"
}

show_help() {
    cat <<EOF
用法: ./run.sh [命令]

  deps             安装 / 更新系统与 Python 依赖
  start            后台启动服务
  stop             停止服务
  restart          重启服务
  status           查看运行和开机自启状态
  logs [行数]      查看最近日志（默认 100 行）
  logs-follow      持续查看日志，Ctrl+C 退出
  autostart-on     安装 systemd 服务、启用开机自启并立即启动
  autostart-off    关闭开机自启并停止 systemd 服务
  foreground       前台运行，便于调试
  menu             显示交互菜单
EOF
}

menu() {
    while true; do
        say ""
        say "========================================"
        say " HeySure CLI Adapter 服务管理"
        say "========================================"
        say " 1) 安装 / 更新依赖"
        say " 2) 启动服务"
        say " 3) 停止服务"
        say " 4) 重启服务"
        say " 5) 查看状态"
        say " 6) 查看最近日志"
        say " 7) 持续查看日志"
        say " 8) 启用开机自启"
        say " 9) 关闭开机自启"
        say " a) 前台启动调试"
        say " 0) 退出"
        printf '请选择: '
        read -r choice || exit 0
        case "$choice" in
            1) install_deps ;;
            2) start_service ;;
            3) stop_service ;;
            4) restart_service ;;
            5) show_status ;;
            6) show_logs 100 ;;
            7) follow_logs ;;
            8) autostart_on ;;
            9) autostart_off ;;
            a|A) foreground ;;
            0) exit 0 ;;
            *) say "无效选项，请重新选择。" ;;
        esac
    done
}

command=${1:-}
if [ -z "$command" ]; then
    if [ -t 0 ]; then
        menu
    else
        foreground
    fi
    exit 0
fi
shift
case "$command" in
    deps|install-deps) install_deps ;;
    start) start_service ;;
    stop) stop_service ;;
    restart) restart_service ;;
    status) show_status ;;
    logs) show_logs "${1:-100}" ;;
    logs-follow|follow) follow_logs ;;
    autostart-on|enable) autostart_on ;;
    autostart-off|disable) autostart_off ;;
    foreground|fg|run) foreground ;;
    menu) menu ;;
    help|-h|--help) show_help ;;
    *) show_help; die "未知命令: $command" ;;
esac
