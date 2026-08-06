"""HeySure web-managed CLI adapter.

This is the single deployment entrypoint for the Codex, Grok and Antigravity
gateways in this directory. It follows device/read.md: login, register a custom
device with self-described MCP tools, receive task:dispatch and reply exactly
once. The selected gateway is started only after the web console sends
``device:cli-config``.
"""

from __future__ import annotations

import argparse
from collections import deque
import json
import os
from pathlib import Path
import platform as host_platform
import re
import socket
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import socketio


BASE_DIR = Path(__file__).resolve().parent
CONTROL_RUNTIME_DIR = BASE_DIR / "control_runtime"
CONFIG_PATH = CONTROL_RUNTIME_DIR / "config.json"
WEB_INDEX_PATH = BASE_DIR / "web" / "index.html"
VERSION = "2.0.0"
PLATFORMS = {"codex", "grok", "antigravity"}
DEFAULT_COMMANDS = {"codex": "codex", "grok": "grok", "antigravity": "agy"}

TOOL_DEFS = [
    {
        "name": "cli.run",
        "description": (
            "让本机已登录的 Codex、Grok 或 Antigravity CLI 完成一个任务并返回文本结果。"
            "适合代码分析、生成、修改和 CLI 自身支持的工作；长任务请传 timeout_seconds（30-900 秒）。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "交给 CLI 的完整任务说明"},
                "model": {"type": "string", "description": "可选，本次覆盖网页配置的模型"},
                "session_id": {"type": "string", "description": "可选，复用 CLI 对话线程的稳定标识"},
                "timeout_seconds": {"type": "integer", "minimum": 30, "maximum": 900},
            },
            "required": ["prompt"],
        },
        "destructive": False,
    },
    {
        "name": "cli.models",
        "description": "列出网页当前选择的本机 CLI 可用模型。",
        "input_schema": {"type": "object", "properties": {}},
        "destructive": False,
    },
    {
        "name": "cli.status",
        "description": "检查 CLI Adapter、所选平台和本机网关是否已就绪。",
        "input_schema": {"type": "object", "properties": {}},
        "destructive": False,
    },
]


def _json_request(url: str, payload: Optional[dict] = None, timeout: float = 10, headers: Optional[dict] = None) -> dict:
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(url, data=body, method="POST" if body is not None else "GET")
    request.add_header("Accept", "application/json")
    if body is not None:
        request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            result = json.loads(raw)
            return result if isinstance(result, dict) else {"data": result}
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[-4000:]
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    except (URLError, TimeoutError, ValueError) as exc:
        raise RuntimeError(str(exc)) from exc


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


class GatewayManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._process: Optional[subprocess.Popen[str]] = None
        self._port = 0
        self._config: Dict[str, Any] = {}
        self._error = "等待网页配置"
        self._logs: deque[str] = deque(maxlen=30)

    def _argv(self, config: dict, port: int) -> List[str]:
        selected = config["platform"]
        command = str(config.get("command") or DEFAULT_COMMANDS[selected])
        timeout = str(config["timeoutSeconds"])
        models = str(config.get("models") or "")
        server = BASE_DIR / f"{selected}_cli_api" / "server.py"
        common = [sys.executable, str(server)]
        if selected == "codex":
            argv = common + ["--host", "127.0.0.1", "--port", str(port), "--timeout", timeout,
                             "--command", command, "--sandbox", config["sandbox"]]
            if models:
                argv += ["--models", models]
            sessions_dir = str(config.get("sessionsDir") or "").strip()
            if not sessions_dir and config.get("workspace"):
                sessions_dir = str(Path(config["workspace"]).expanduser() / ".heysure-cli" / "codex")
            if sessions_dir:
                argv += ["--sessions-dir", str(Path(sessions_dir).expanduser())]
            return argv
        if selected == "grok":
            argv = common + ["--host", "127.0.0.1", "--port", str(port), "--timeout", timeout,
                             "--command", command]
            if models:
                argv += ["--models", models]
            if config.get("workspace"):
                argv += ["--cwd", str(Path(config["workspace"]).expanduser())]
            return argv
        backend = str(config.get("backend") or "cli")
        argv = common + ["serve", "--host", "127.0.0.1", "--port", str(port), "--timeout", timeout,
                         "--backend", backend, "--cli-command", command,
                         "--cli-arg-safe-bytes", str(config.get("argSafeBytes") or 98304)]
        if models:
            argv += ["--models", models]
        sessions_dir = str(config.get("sessionsDir") or "").strip()
        if not sessions_dir and config.get("workspace"):
            sessions_dir = str(Path(config["workspace"]).expanduser() / ".heysure-cli" / "antigravity")
        if sessions_dir:
            argv += ["--cli-sessions-dir", str(Path(sessions_dir).expanduser())]
        if config.get("authFile"):
            argv += ["--auth-file", str(Path(config["authFile"]).expanduser())]
        if config.get("callbackPort"):
            argv += ["--callback-port", str(config["callbackPort"])]
        return argv

    def _child_env(self, config: dict) -> dict:
        env = dict(os.environ)
        proxy = str(config.get("proxyUrl") or "").strip()
        if proxy:
            for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
                env[key] = proxy
        no_proxy = str(config.get("noProxy") or "localhost,127.0.0.1,::1").strip()
        env["NO_PROXY"] = no_proxy
        env["no_proxy"] = no_proxy
        if config["platform"] == "grok":
            env["GROK_CLI_ACP"] = "1" if config.get("acpEnabled", True) else "0"
            env["GROK_CLI_TOOL_GRACE"] = str(config.get("toolGraceSeconds") or 0.5)
            env["GROK_CLI_SESSION_TTL"] = str(config.get("sessionTtlSeconds") or 1800)
            env["GROK_CLI_MAX_SESSIONS"] = str(config.get("maxSessions") or 6)
            if config.get("xaiApiKey"):
                env["XAI_API_KEY"] = str(config["xaiApiKey"])
        if config["platform"] == "antigravity":
            for source, target in (
                ("oauthClientId", "ANTIGRAVITY_OAUTH_CLIENT_ID"),
                ("oauthClientSecret", "ANTIGRAVITY_OAUTH_CLIENT_SECRET"),
                ("baseUrls", "ANTIGRAVITY_BASE_URLS"),
            ):
                if config.get(source):
                    env[target] = str(config[source])
        return env

    def _read_logs(self, process: subprocess.Popen[str]) -> None:
        if process.stdout is None:
            return
        for line in process.stdout:
            clean = line.rstrip()
            if clean:
                self._logs.append(clean[-1000:])

    def stop(self) -> None:
        with self._lock:
            process, self._process = self._process, None
            self._port = 0
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def apply(self, raw: dict) -> None:
        selected = str(raw.get("platform") or "").strip().lower()
        if selected not in PLATFORMS:
            raise ValueError(f"不支持的 CLI 平台: {selected}")
        config = {
            "platform": selected,
            "command": str(raw.get("command") or "").strip(),
            "model": str(raw.get("model") or "").strip(),
            "models": str(raw.get("models") or "").strip(),
            "workspace": str(raw.get("workspace") or "").strip(),
            "sessionsDir": str(raw.get("sessionsDir") or "").strip(),
            "timeoutSeconds": max(30, min(int(raw.get("timeoutSeconds") or 300), 900)),
            "sandbox": str(raw.get("sandbox") or "read-only"),
            "proxyUrl": str(raw.get("proxyUrl") or "").strip(),
            "noProxy": str(raw.get("noProxy") or "localhost,127.0.0.1,::1").strip(),
            "acpEnabled": bool(raw.get("acpEnabled", True)),
            "toolGraceSeconds": max(0.05, min(float(raw.get("toolGraceSeconds") or 0.5), 10.0)),
            "sessionTtlSeconds": max(60, min(int(raw.get("sessionTtlSeconds") or 1800), 86400)),
            "maxSessions": max(1, min(int(raw.get("maxSessions") or 6), 100)),
            "xaiApiKey": str(raw.get("xaiApiKey") or ""),
            "backend": str(raw.get("backend") or "cli"),
            "argSafeBytes": max(8192, min(int(raw.get("argSafeBytes") or 98304), 1048576)),
            "authFile": str(raw.get("authFile") or "").strip(),
            "callbackPort": max(1024, min(int(raw.get("callbackPort") or 51121), 65535)),
            "oauthClientId": str(raw.get("oauthClientId") or "").strip(),
            "oauthClientSecret": str(raw.get("oauthClientSecret") or ""),
            "baseUrls": str(raw.get("baseUrls") or "").strip(),
            "enabled": bool(raw.get("enabled", True)),
            "configured": bool(raw.get("configured", True)),
        }
        with self._lock:
            if config == self._config and self._process and self._process.poll() is None:
                return
        self.stop()
        with self._lock:
            self._config = config
            self._error = ""
        if not config["configured"]:
            self._error = "请先在 HeySure 网页的设备卡片中保存 CLI 设置"
            return
        if not config["enabled"]:
            self._error = "CLI Adapter 已在网页中停用"
            return
        port = _free_port()
        argv = self._argv(config, port)
        cwd = str(Path(config["workspace"]).expanduser()) if config.get("workspace") else str(BASE_DIR)
        if not Path(cwd).is_dir():
            self._error = f"工作目录不存在: {cwd}"
            return
        try:
            process = subprocess.Popen(
                argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                env=self._child_env(config),
            )
        except OSError as exc:
            self._error = f"启动 {selected} 网关失败: {exc}"
            return
        with self._lock:
            self._process, self._port = process, port
        threading.Thread(target=self._read_logs, args=(process,), daemon=True).start()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self._error = f"{selected} 网关提前退出（{process.returncode}）"
                return
            try:
                _json_request(f"http://127.0.0.1:{port}/health", timeout=1)
                return
            except RuntimeError:
                time.sleep(0.25)
        self._error = f"{selected} 网关启动超时"
        self.stop()

    def status(self) -> dict:
        with self._lock:
            running = bool(self._process and self._process.poll() is None and self._port)
            return {
                "ready": running and not self._error,
                "platform": self._config.get("platform"),
                "model": self._config.get("model") or "default",
                "enabled": self._config.get("enabled", False),
                "configured": self._config.get("configured", False),
                "error": self._error or None,
                "recentLogs": list(self._logs)[-20:],
            }

    def restart(self) -> None:
        with self._lock:
            config = dict(self._config)
            self._config = {}
        self.stop()
        self.apply(config)

    def request(self, path: str, payload: Optional[dict] = None, timeout: Optional[int] = None, headers: Optional[dict] = None) -> dict:
        with self._lock:
            process, port, error = self._process, self._port, self._error
            configured_timeout = int(self._config.get("timeoutSeconds") or 300)
        if not process or process.poll() is not None or not port or error:
            raise RuntimeError(error or "CLI 网关未运行")
        return _json_request(
            f"http://127.0.0.1:{port}{path}", payload,
            timeout=timeout or configured_timeout, headers=headers,
        )


class CliAdapter:
    def __init__(self, config: dict) -> None:
        self.server = ""
        self.account = ""
        self.password = ""
        self.device_id = ""
        self.name = ""
        self.token = ""
        self.socket_url = ""
        self.registered = False
        self.gateway = GatewayManager()
        self.sio = socketio.Client(reconnection=True, reconnection_delay=2, logger=False)
        self._connection_lock = threading.RLock()
        self._connection_key: tuple[str, ...] = ()
        self._bind_events()
        self.apply_config(config)

    def login(self) -> None:
        data = _json_request(
            f"{self.server}/api/auth/login",
            {"account": self.account, "password": self.password}, timeout=15,
        )
        self.token = str(data.get("access_token") or "")
        if not self.token:
            raise RuntimeError("登录响应缺少 access_token")
        self.socket_url = str(data.get("agent_socket_url") or self.server).rstrip("/")

    def register(self) -> None:
        self.sio.emit("device:register", {
            "id": self.device_id,
            "name": self.name,
            "platform": "heysure-cli-adapter",
            "deviceType": "custom",
            "cliAdapter": True,
            "token": self.token,
            "version": VERSION,
            "capabilities": [item["name"] for item in TOOL_DEFS],
            "toolDefs": TOOL_DEFS,
            "os": {
                "platform": host_platform.system().lower(),
                "arch": host_platform.machine(),
                "hostname": socket.gethostname(),
            },
        })

    def _bind_events(self) -> None:
        @self.sio.event
        def connect() -> None:
            self.registered = False
            self.register()

            def retry() -> None:
                while self.sio.connected and not self.registered:
                    self.sio.sleep(3)
                    if not self.registered:
                        self.register()

            self.sio.start_background_task(retry)

        @self.sio.on("device:registered")
        def registered(data: dict) -> None:
            self.registered = True
            print(f"已注册 {self.device_id}，绑定 AI: {data.get('aiConfigId') or '未绑定'}", flush=True)

        @self.sio.on("device:register_rejected")
        def rejected(data: dict) -> None:
            print(f"注册被拒绝，重新登录: {data.get('reason')}", flush=True)
            try:
                self.login()
                self.register()
            except Exception as exc:
                print(f"重新登录失败: {exc}", flush=True)

        @self.sio.on("task:dispatch")
        def task(task: dict) -> None:
            threading.Thread(target=self._handle_task, args=(task,), daemon=True).start()

    def apply_config(self, data: dict) -> None:
        self.server = str(data.get("server") or "").strip().rstrip("/")
        self.account = str(data.get("account") or "").strip()
        self.password = str(data.get("password") or "")
        self.device_id = str(data.get("deviceId") or _default_device_id()).strip()
        self.name = str(data.get("name") or "本机 CLI Adapter").strip()
        self.gateway.apply({**data, "configured": True})
        key = (self.server, self.account, self.password, self.device_id)
        with self._connection_lock:
            if key == self._connection_key and self.sio.connected:
                self.register()
                return
            self._connection_key = key
            if self.sio.connected:
                self.sio.disconnect()
            self.registered = False
        if self.server and self.account and self.password:
            threading.Thread(target=self._connect, daemon=True).start()

    def _connect(self) -> None:
        try:
            self.login()
            self.sio.connect(self.socket_url, transports=["websocket", "polling"], wait_timeout=15)
        except Exception as exc:
            print(f"连接 HeySure 失败: {exc}", flush=True)

    def _execute(self, tool: str, args: dict, task: dict) -> tuple[Any, str]:
        if tool == "cli.status":
            result = self.gateway.status()
            return result, "CLI Adapter 已就绪" if result["ready"] else f"CLI Adapter 未就绪：{result.get('error')}"
        if tool == "cli.models":
            result = self.gateway.request("/v1/models", timeout=30)
            models = [str(item.get("id")) for item in result.get("data", []) if isinstance(item, dict) and item.get("id")]
            return {"models": models, "platform": self.gateway.status().get("platform")}, f"发现 {len(models)} 个模型"
        if tool != "cli.run":
            raise ValueError(f"unknown tool: {tool}")
        prompt = str(args.get("prompt") or "").strip()
        if not prompt:
            raise ValueError("prompt 不能为空")
        configured_model = str(self.gateway.status().get("model") or "").strip()
        if configured_model == "default":
            configured_model = ""
        model = str(args.get("model") or configured_model).strip()
        session_id = str(args.get("session_id") or task.get("sessionId") or task.get("taskId") or "")
        timeout = max(30, min(int(args.get("timeout_seconds") or 300), 900))
        payload = {"messages": [{"role": "user", "content": prompt}], "stream": False, "user": session_id}
        if model:
            payload["model"] = model
        response = self.gateway.request(
            "/v1/chat/completions", payload,
            timeout=timeout,
            headers={"X-HeySure-Session-ID": session_id},
        )
        choices = response.get("choices") or []
        message = choices[0].get("message") if choices and isinstance(choices[0], dict) else {}
        content = message.get("content") if isinstance(message, dict) else None
        if content is None:
            content = json.dumps(message or response, ensure_ascii=False)
        return {"content": content, "model": response.get("model"), "usage": response.get("usage")}, str(content)[:500]

    def _handle_task(self, task: dict) -> None:
        task_id = str(task.get("taskId") or "")
        tool = str(task.get("tool") or "")
        try:
            self.sio.emit("task:progress", {"taskId": task_id, "deviceId": self.device_id, "message": f"正在调用 {tool}"})
            result, summary = self._execute(tool, task.get("args") or {}, task)
            self.sio.emit("task:result", {
                "taskId": task_id, "deviceId": self.device_id, "success": True,
                "tool": tool, "result": result, "summary": summary,
            })
        except Exception as exc:
            self.sio.emit("task:error", {"taskId": task_id, "deviceId": self.device_id, "error": str(exc)})

    def status(self) -> dict:
        return {
            "heysureConnected": self.sio.connected,
            "registered": self.registered,
            "deviceId": self.device_id,
            "gateway": self.gateway.status(),
        }


def _default_device_id() -> str:
    hostname = re.sub(r"[^a-zA-Z0-9_.-]+", "-", socket.gethostname()).strip("-") or "local"
    return f"heysure-cli-{hostname.lower()}"


def _default_config() -> dict:
    profiles = {
        "codex": {
            "command": os.getenv("CODEX_CLI_COMMAND", ""), "models": os.getenv("CODEX_CLI_MODELS", ""),
            "model": "", "workspace": "", "sessionsDir": os.getenv("CODEX_CLI_SESSIONS_DIR", ""),
            "timeoutSeconds": int(os.getenv("CODEX_CLI_TIMEOUT", "900")), "sandbox": os.getenv("CODEX_CLI_SANDBOX", "read-only"),
            "proxyUrl": "", "noProxy": "localhost,127.0.0.1,::1",
        },
        "grok": {
            "command": os.getenv("GROK_CLI_COMMAND", ""), "models": os.getenv("GROK_CLI_MODELS", "grok-4.5"),
            "model": "grok-4.5", "workspace": os.getenv("GROK_CLI_CWD", ""), "sessionsDir": "",
            "timeoutSeconds": int(os.getenv("GROK_CLI_TIMEOUT", "600")), "sandbox": "read-only",
            "acpEnabled": os.getenv("GROK_CLI_ACP", "1").lower() not in {"0", "false", "no"},
            "toolGraceSeconds": float(os.getenv("GROK_CLI_TOOL_GRACE", "0.5")),
            "sessionTtlSeconds": int(os.getenv("GROK_CLI_SESSION_TTL", "1800")),
            "maxSessions": int(os.getenv("GROK_CLI_MAX_SESSIONS", "6")),
            "xaiApiKey": os.getenv("XAI_API_KEY", ""), "proxyUrl": "", "noProxy": "localhost,127.0.0.1,::1",
        },
        "antigravity": {
            "command": os.getenv("ANTIGRAVITY_CLI_COMMAND", ""), "models": os.getenv("ANTIGRAVITY_MODELS", "gemini-3.5-flash-medium,gemini-3.5-flash-high,gemini-3.5-flash-low,gemini-3.1-pro-low,gemini-3.1-pro-high"),
            "model": "gemini-3.5-flash-medium", "workspace": "", "sessionsDir": os.getenv("ANTIGRAVITY_CLI_SESSIONS_DIR", ""),
            "timeoutSeconds": int(os.getenv("ANTIGRAVITY_TIMEOUT", "600")), "sandbox": "read-only",
            "backend": os.getenv("ANTIGRAVITY_BACKEND", "cli"), "argSafeBytes": int(os.getenv("ANTIGRAVITY_CLI_ARG_SAFE_BYTES", "98304")),
            "authFile": os.getenv("ANTIGRAVITY_AUTH_FILE", ""), "callbackPort": int(os.getenv("ANTIGRAVITY_OAUTH_CALLBACK_PORT", "51121")),
            "oauthClientId": os.getenv("ANTIGRAVITY_OAUTH_CLIENT_ID", ""), "oauthClientSecret": os.getenv("ANTIGRAVITY_OAUTH_CLIENT_SECRET", ""),
            "baseUrls": os.getenv("ANTIGRAVITY_BASE_URLS", ""), "proxyUrl": "", "noProxy": "localhost,127.0.0.1,::1",
        },
    }
    result = {
        "server": os.getenv("HEYSURE_SERVER", "http://127.0.0.1:3000"),
        "account": os.getenv("HEYSURE_ACCOUNT", ""),
        "password": os.getenv("HEYSURE_PASSWORD", ""),
        "deviceId": os.getenv("HEYSURE_CLI_DEVICE_ID", _default_device_id()),
        "name": os.getenv("HEYSURE_CLI_DEVICE_NAME", "本机 CLI Adapter"),
        "platform": "codex",
        "profiles": profiles,
        "enabled": False,
    }
    result.update(profiles["codex"])
    return result


def _normalized_profile(platform: str, raw: dict, previous: dict) -> dict:
    sandbox = str(raw.get("sandbox", previous.get("sandbox", "read-only"))).strip().lower()
    if sandbox not in {"read-only", "workspace-write", "danger-full-access"}:
        raise ValueError("无效的 Codex sandbox")
    backend = str(raw.get("backend", previous.get("backend", "cli"))).strip().lower()
    if backend not in {"cli", "direct"}:
        raise ValueError("Antigravity backend 仅支持 cli/direct")
    def secret(name: str) -> str:
        return str(raw.get(name) or previous.get(name) or "")
    return {
        "command": str(raw.get("command", previous.get("command", ""))).strip(),
        "models": str(raw.get("models", previous.get("models", ""))).strip(),
        "model": str(raw.get("model", previous.get("model", ""))).strip(),
        "workspace": str(raw.get("workspace", previous.get("workspace", ""))).strip(),
        "sessionsDir": str(raw.get("sessionsDir", previous.get("sessionsDir", ""))).strip(),
        "timeoutSeconds": max(30, min(int(raw.get("timeoutSeconds", previous.get("timeoutSeconds", 600))), 900)),
        "sandbox": sandbox,
        "proxyUrl": secret("proxyUrl").strip(),
        "noProxy": str(raw.get("noProxy", previous.get("noProxy", "localhost,127.0.0.1,::1"))).strip(),
        "acpEnabled": bool(raw.get("acpEnabled", previous.get("acpEnabled", True))),
        "toolGraceSeconds": max(0.05, min(float(raw.get("toolGraceSeconds", previous.get("toolGraceSeconds", 0.5))), 10.0)),
        "sessionTtlSeconds": max(60, min(int(raw.get("sessionTtlSeconds", previous.get("sessionTtlSeconds", 1800))), 86400)),
        "maxSessions": max(1, min(int(raw.get("maxSessions", previous.get("maxSessions", 6))), 100)),
        "xaiApiKey": secret("xaiApiKey"),
        "backend": backend,
        "argSafeBytes": max(8192, min(int(raw.get("argSafeBytes", previous.get("argSafeBytes", 98304))), 1048576)),
        "authFile": str(raw.get("authFile", previous.get("authFile", ""))).strip(),
        "callbackPort": max(1024, min(int(raw.get("callbackPort", previous.get("callbackPort", 51121))), 65535)),
        "oauthClientId": str(raw.get("oauthClientId", previous.get("oauthClientId", ""))).strip(),
        "oauthClientSecret": secret("oauthClientSecret"),
        "baseUrls": str(raw.get("baseUrls", previous.get("baseUrls", ""))).strip(),
    }


def _normalize_config(raw: dict, previous: Optional[dict] = None) -> dict:
    defaults = _default_config()
    previous = previous or defaults
    platform = str(raw.get("platform", previous.get("platform", "codex"))).strip().lower()
    if platform not in PLATFORMS:
        raise ValueError("platform 仅支持 codex、grok、antigravity")
    password = str(raw.get("password") or "")
    if not password:
        password = str(previous.get("password") or "")
    previous_profiles = previous.get("profiles") if isinstance(previous.get("profiles"), dict) else {}
    raw_profiles = raw.get("profiles") if isinstance(raw.get("profiles"), dict) else {}
    profiles = {}
    for name in sorted(PLATFORMS):
        base = dict(defaults["profiles"][name])
        if isinstance(previous_profiles.get(name), dict):
            base.update(previous_profiles[name])
        elif name == previous.get("platform"):
            base.update({key: previous[key] for key in base if key in previous})
        candidate = raw_profiles.get(name) if isinstance(raw_profiles.get(name), dict) else {}
        if name == platform and not raw_profiles:
            candidate = {key: raw[key] for key in base if key in raw}
        profiles[name] = _normalized_profile(name, candidate, base)
    result = {
        "server": str(raw.get("server", previous.get("server", ""))).strip().rstrip("/"),
        "account": str(raw.get("account", previous.get("account", ""))).strip(),
        "password": password,
        "deviceId": str(raw.get("deviceId", previous.get("deviceId", _default_device_id()))).strip(),
        "name": str(raw.get("name", previous.get("name", "本机 CLI Adapter"))).strip(),
        "platform": platform,
        "profiles": profiles,
        "enabled": bool(raw.get("enabled", previous.get("enabled", True))),
    }
    result.update(profiles[platform])
    if not result["server"] or not result["account"] or not result["deviceId"]:
        raise ValueError("HeySure 地址、账号和设备 ID 不能为空")
    return result


def _load_config() -> dict:
    defaults = _default_config()
    try:
        saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return _normalize_config(saved, defaults)
    except FileNotFoundError:
        return defaults
    except Exception as exc:
        print(f"读取配置失败，使用默认值: {exc}", flush=True)
        return defaults


def _save_config(config: dict) -> None:
    CONTROL_RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    temporary = CONFIG_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, CONFIG_PATH)
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass


class ControlApp:
    def __init__(self, config: dict) -> None:
        self._lock = threading.RLock()
        self.config = config
        self.adapter = CliAdapter(config)

    def public_config(self) -> dict:
        with self._lock:
            value = json.loads(json.dumps(self.config))
            value["passwordConfigured"] = bool(value.get("password"))
            value["password"] = ""
            for profile in value.get("profiles", {}).values():
                for key in ("xaiApiKey", "oauthClientSecret"):
                    profile[f"{key}Configured"] = bool(profile.get(key))
                    profile[key] = ""
                # Proxy URLs may embed credentials; do not echo them back.
                profile["proxyUrlConfigured"] = bool(profile.get("proxyUrl"))
                profile["proxyUrl"] = ""
            for key in ("xaiApiKey", "oauthClientSecret", "proxyUrl"):
                value.pop(key, None)
            return value

    def update(self, raw: dict) -> dict:
        with self._lock:
            config = _normalize_config(raw, self.config)
            _save_config(config)
            self.config = config
        self.adapter.apply_config(config)
        return self.public_config()

    def status(self) -> dict:
        return self.adapter.status()

    def action(self, action: str, payload: dict) -> dict:
        if action == "models":
            return self.adapter.gateway.request("/v1/models", timeout=30)
        if action == "test":
            prompt = str(payload.get("prompt") or "").strip()
            if not prompt:
                raise ValueError("测试提示词不能为空")
            model = str(payload.get("model") or self.config.get("model") or "").strip()
            body: Dict[str, Any] = {
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "user": "web-control-test",
            }
            if model:
                body["model"] = model
            return self.adapter.gateway.request(
                "/v1/chat/completions", body,
                timeout=int(self.config.get("timeoutSeconds") or 300),
                headers={"X-HeySure-Session-ID": "web-control-test"},
            )
        if action == "restart":
            self.adapter.gateway.restart()
            return self.adapter.gateway.status()
        if action == "probe":
            command = str(self.config.get("command") or DEFAULT_COMMANDS[self.config["platform"]])
            try:
                completed = subprocess.run(
                    [command, "--version"], cwd=self.config.get("workspace") or str(BASE_DIR),
                    env=self.adapter.gateway._child_env(self.config), capture_output=True,
                    text=True, encoding="utf-8", errors="replace", timeout=20, check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise RuntimeError(f"CLI 探测失败: {exc}") from exc
            return {
                "command": command, "exitCode": completed.returncode,
                "output": (completed.stdout or completed.stderr or "").strip()[-4000:],
            }
        raise ValueError(f"未知操作: {action}")


class ControlHandler(BaseHTTPRequestHandler):
    app: ControlApp

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[control] {self.address_string()} {fmt % args}", flush=True)

    def _json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/":
            try:
                data = WEB_INDEX_PATH.read_bytes()
            except OSError as exc:
                self._json(500, {"error": f"管理页面缺失: {exc}"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif path == "/api/config":
            self._json(200, self.app.public_config())
        elif path == "/api/status":
            self._json(200, self.app.status())
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0].rstrip("/")
        if path not in {"/api/config", "/api/action"}:
            self._json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 1024 * 1024:
                raise ValueError("请求体为空或过大")
            raw = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("请求体必须是 JSON 对象")
            if path == "/api/config":
                self._json(200, {"ok": True, "config": self.app.update(raw)})
            else:
                action = str(raw.get("action") or "").strip()
                self._json(200, {"ok": True, "result": self.app.action(action, raw)})
        except Exception as exc:
            self._json(400, {"ok": False, "error": str(exc)})


def main() -> int:
    parser = argparse.ArgumentParser(description="HeySure 网页控制的多平台 CLI Adapter")
    parser.add_argument("--host", default=os.getenv("HEYSURE_CLI_CONTROL_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("HEYSURE_CLI_CONTROL_PORT", "8130")))
    args = parser.parse_args()
    config = _load_config()
    app = ControlApp(config)
    ControlHandler.app = app
    server = ThreadingHTTPServer((args.host, args.port), ControlHandler)
    server.daemon_threads = True
    print(f"CLI Adapter 管理页: http://{args.host}:{args.port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if app.adapter.sio.connected:
            app.adapter.sio.disconnect()
        app.adapter.gateway.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
