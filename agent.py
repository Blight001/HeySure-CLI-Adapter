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
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from urllib.request import ProxyHandler, build_opener

import socketio


BASE_DIR = Path(__file__).resolve().parent
CONTROL_RUNTIME_DIR = BASE_DIR / "control_runtime"
CONFIG_PATH = CONTROL_RUNTIME_DIR / "config.json"
WEB_INDEX_PATH = BASE_DIR / "web" / "index.html"
VERSION = "2.0.0"
PLATFORMS = ("codex", "grok", "antigravity")
DEFAULT_COMMANDS = {"codex": "codex", "grok": "grok", "antigravity": "agy"}
DEFAULT_PORTS = {"codex": 8120, "grok": 8100, "antigravity": 8110}
DEVICE_CONFIG_PATH = BASE_DIR.parent / "device.config.json"
PRODUCTION_SERVER_FALLBACK = "http://49.234.181.190:58150"
LOCAL_TEST_SERVER_FALLBACK = "http://127.0.0.1:3000"


def _configured_server_url() -> str:
    try:
        device_config = json.loads(DEVICE_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        device_config = {}
    local_test = os.getenv("HEYSURE_LOCAL_TEST", "").strip().lower() in {"1", "true", "yes", "on"}
    key = "local_test_server_url" if local_test else "default_server_url"
    fallback = LOCAL_TEST_SERVER_FALLBACK if local_test else PRODUCTION_SERVER_FALLBACK
    value = device_config.get(key, fallback)
    return value.strip() if isinstance(value, str) and value.strip() else fallback

TOOL_DEFS = [
    {
        "name": "cli.run",
        "description": (
            "通过统一网关让本机已登录的 Codex、Grok 或 Antigravity CLI 完成任务。"
            "适合代码分析、生成、修改和 CLI 自身支持的工作；长任务请传 timeout_seconds（30-900 秒）。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "交给 CLI 的完整任务说明"},
                "model": {"type": "string", "description": "可选，本次覆盖网页配置的模型"},
                "platform": {"type": "string", "enum": ["codex", "grok", "antigravity"], "description": "可选，强制使用指定 CLI；不传则按模型自动路由"},
                "session_id": {"type": "string", "description": "可选，复用 CLI 对话线程的稳定标识"},
                "timeout_seconds": {"type": "integer", "minimum": 30, "maximum": 900},
            },
            "required": ["prompt"],
        },
        "destructive": False,
    },
    {
        "name": "cli.models",
        "description": "列出统一网关中所有已启用 CLI 的可用模型及所属平台。",
        "input_schema": {"type": "object", "properties": {}},
        "destructive": False,
    },
    {
        "name": "cli.status",
        "description": "检查统一网关及 Codex、Grok、Antigravity 各后端是否已就绪。",
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
        try:
            detail = exc.read().decode("utf-8", errors="replace")[-4000:]
        finally:
            exc.close()
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    except (URLError, TimeoutError, ValueError) as exc:
        raise RuntimeError(str(exc)) from exc


def _server_url(raw: Any) -> str:
    """Normalize a user-entered HeySure server origin."""
    value = str(raw or "").strip().rstrip("/")
    if value and "://" not in value:
        value = "http://" + value
    parsed = urlsplit(value)
    if not value or parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("HeySure 服务器地址无效，请填写 http://域名或IP:端口")
    if parsed.query or parsed.fragment:
        raise ValueError("HeySure 服务器地址不要包含查询参数或片段")
    return value


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
        server = BASE_DIR / "server.py"
        common = [sys.executable, str(server), "--platform", selected]
        host = str(config.get("listenHost") or ("0.0.0.0" if config.get("exposeEnabled") else "127.0.0.1"))
        if selected == "codex":
            argv = common + ["--host", host, "--port", str(port), "--timeout", timeout,
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
            argv = common + ["--host", host, "--port", str(port), "--timeout", timeout,
                             "--command", command]
            if models:
                argv += ["--models", models]
            if config.get("workspace"):
                argv += ["--cwd", str(Path(config["workspace"]).expanduser())]
            return argv
        backend = str(config.get("backend") or "cli")
        argv = common + ["serve", "--host", host, "--port", str(port), "--timeout", timeout,
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
        gateway_key = str(config.get("gatewayApiKey") or "")
        if gateway_key:
            env[{"codex": "CODEX_CLI_API_KEY", "grok": "GROK_CLI_API_KEY", "antigravity": "ANTIGRAVITY_API_KEY"}[config["platform"]]] = gateway_key
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
            "exposeEnabled": bool(raw.get("exposeEnabled", False)),
            "listenHost": str(raw.get("listenHost") or "").strip(),
            "listenPort": max(1024, min(int(raw.get("listenPort") or DEFAULT_PORTS[selected]), 65535)),
            "gatewayApiKey": str(raw.get("gatewayApiKey") or ""),
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
        if config["exposeEnabled"] and not config["gatewayApiKey"]:
            self._error = "对外开放时必须设置网关 API Key"
            return
        port = config["listenPort"] if config["exposeEnabled"] else _free_port()
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
                headers = {"Authorization": f"Bearer {config['gatewayApiKey']}"} if config["gatewayApiKey"] else None
                _json_request(f"http://127.0.0.1:{port}/health", timeout=1, headers=headers)
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
                "listenHost": self._config.get("listenHost") or ("0.0.0.0" if self._config.get("exposeEnabled") else "127.0.0.1"),
                "listenPort": self._port or self._config.get("listenPort"),
                "exposeEnabled": self._config.get("exposeEnabled", False),
            }

    def request(self, path: str, payload: Optional[dict] = None, timeout: Optional[int] = None, headers: Optional[dict] = None) -> dict:
        with self._lock:
            process, port, error = self._process, self._port, self._error
            configured_timeout = int(self._config.get("timeoutSeconds") or 300)
            api_key = str(self._config.get("gatewayApiKey") or "")
        if not process or process.poll() is not None or not port or error:
            raise RuntimeError(error or "CLI 网关未运行")
        merged_headers = dict(headers or {})
        if api_key:
            merged_headers["Authorization"] = f"Bearer {api_key}"
        return _json_request(
            f"http://127.0.0.1:{port}{path}", payload,
            timeout=timeout or configured_timeout, headers=merged_headers,
        )

    def restart(self) -> None:
        with self._lock:
            config = dict(self._config)
            self._config = {}
        self.stop()
        self.apply(config)


class UnifiedGatewayFleet:
    """Runs enabled platform gateways privately and exposes one routed API."""

    def __init__(self) -> None:
        self.managers = {name: GatewayManager() for name in PLATFORMS}
        self._lock = threading.RLock()
        self._config: Dict[str, Any] = {}
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._error = ""

    def _private_config(self, platform: str, config: dict) -> dict:
        profile = dict(config.get("profiles", {}).get(platform) or {})
        return {
            **profile,
            "platform": platform,
            "enabled": bool(config.get("enabled")) and bool(profile.get("platformEnabled", False)),
            "configured": True,
            "exposeEnabled": False,
            "listenHost": "127.0.0.1",
            "gatewayApiKey": "",
        }

    def _stop_api(self) -> None:
        with self._lock:
            server, self._server = self._server, None
            self._thread = None
        if server:
            server.shutdown()
            server.server_close()

    def _start_api(self, config: dict) -> None:
        if not config.get("enabled"):
            return
        host = str(config.get("gatewayHost") or "127.0.0.1")
        port = int(config.get("gatewayPort") or 8140)
        fleet = self

        class UnifiedHandler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: Any) -> None:
                return

            def _send(self, status: int, value: dict) -> None:
                data = json.dumps(value, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _authorized(self) -> bool:
                key = str(fleet._config.get("gatewayApiKey") or "")
                return not key or self.headers.get("Authorization", "") == f"Bearer {key}"

            def do_GET(self) -> None:
                if not self._authorized():
                    self._send(401, {"error": {"message": "Unauthorized", "type": "authentication_error"}})
                    return
                path = self.path.split("?", 1)[0].rstrip("/")
                try:
                    if path in ("", "/health"):
                        self._send(200, fleet.health())
                    elif path == "/v1/models":
                        self._send(200, fleet.models())
                    else:
                        self._send(404, {"error": {"message": "Not found", "type": "not_found"}})
                except Exception as exc:
                    self._send(502, {"error": {"message": str(exc), "type": "gateway_error"}})

            def do_POST(self) -> None:
                if not self._authorized():
                    self._send(401, {"error": {"message": "Unauthorized", "type": "authentication_error"}})
                    return
                if self.path.split("?", 1)[0].rstrip("/") != "/v1/chat/completions":
                    self._send(404, {"error": {"message": "Not found", "type": "not_found"}})
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > 64 * 1024 * 1024:
                        raise ValueError("请求体为空或过大")
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    forwarded_headers = {
                        name: self.headers.get(name, "")
                        for name in (
                            "X-HeySure-Session-ID",
                            "X-HeySure-History-Mode",
                            "X-HeySure-Context-Revision",
                            "X-HeySure-Request-ID",
                        )
                    }
                    if payload.get("stream"):
                        fleet.stream(self, payload, headers=forwarded_headers)
                        return
                    self._send(200, fleet.complete(payload, headers=forwarded_headers))
                except ValueError as exc:
                    self._send(400, {"error": {"message": str(exc), "type": "invalid_request_error"}})
                except Exception as exc:
                    self._send(502, {"error": {"message": str(exc), "type": "gateway_error"}})

        server_type = ThreadingHTTPServer
        if ":" in host:
            class IPv6ThreadingHTTPServer(ThreadingHTTPServer):
                address_family = socket.AF_INET6
            server_type = IPv6ThreadingHTTPServer
        self._server = server_type((host, port), UnifiedHandler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def apply(self, config: dict) -> None:
        with self._lock:
            old_api = (self._config.get("gatewayHost"), self._config.get("gatewayPort"), self._config.get("enabled"))
            new_api = (config.get("gatewayHost"), config.get("gatewayPort"), config.get("enabled"))
            self._config = config
        for platform, manager in self.managers.items():
            manager.apply(self._private_config(platform, config))
        if old_api != new_api or (config.get("enabled") and self._server is None):
            self._stop_api()
            try:
                self._start_api(config)
                self._error = ""
            except OSError as exc:
                self._error = f"统一 API 启动失败: {exc}"

    def _enabled_platforms(self) -> List[str]:
        return [name for name, manager in self.managers.items() if manager.status().get("ready")]

    def _model_platform(self, model: str, explicit: str = "") -> str:
        if explicit in PLATFORMS:
            return explicit
        if explicit:
            raise ValueError("cli_platform 仅支持 codex、grok、antigravity")
        needle = str(model or "").strip().lower()
        for platform in PLATFORMS:
            profile = self._config.get("profiles", {}).get(platform, {})
            configured = [item.strip().lower() for item in str(profile.get("models") or "").split(",") if item.strip()]
            if profile.get("model"):
                configured.append(str(profile["model"]).strip().lower())
            if needle and needle in configured:
                return platform
        if needle.startswith("grok"):
            return "grok"
        if needle.startswith(("gemini", "antigravity")):
            return "antigravity"
        if needle.startswith(("codex", "gpt-")):
            return "codex"
        default = str(self._config.get("defaultPlatform") or "codex")
        ready = self._enabled_platforms()
        return default if default in ready else (ready[0] if ready else default)

    def complete(self, payload: dict, headers: Optional[dict] = None) -> dict:
        body = dict(payload)
        explicit = str(body.pop("cli_platform", body.pop("_cli_platform", "")) or "").strip().lower()
        platform = self._model_platform(str(body.get("model") or ""), explicit)
        manager = self.managers[platform]
        if not manager.status().get("ready"):
            raise RuntimeError(f"{platform} 后端未启用或未就绪")
        result = manager.request("/v1/chat/completions", body, headers=headers)
        result["cli_platform"] = platform
        return result

    def stream(self, handler: BaseHTTPRequestHandler, payload: dict, headers: Optional[dict] = None) -> None:
        body = dict(payload)
        explicit = str(body.pop("cli_platform", body.pop("_cli_platform", "")) or "").strip().lower()
        platform = self._model_platform(str(body.get("model") or ""), explicit)
        manager = self.managers[platform]
        with manager._lock:
            process, port, error = manager._process, manager._port, manager._error
            timeout = int(manager._config.get("timeoutSeconds") or 300)
        if not process or process.poll() is not None or not port or error:
            raise RuntimeError(error or f"{platform} 后端未启用或未就绪")
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = Request(f"http://127.0.0.1:{port}/v1/chat/completions", data=data, method="POST")
        request.add_header("Content-Type", "application/json")
        request.add_header("Accept", "text/event-stream")
        for key, value in (headers or {}).items():
            if value:
                request.add_header(key, value)
        with urlopen(request, timeout=timeout) as response:
            handler.send_response(response.status)
            handler.send_header("Content-Type", response.headers.get("Content-Type", "text/event-stream; charset=utf-8"))
            handler.send_header("Cache-Control", "no-cache")
            handler.send_header("X-CLI-Platform", platform)
            handler.send_header("Connection", "close")
            handler.end_headers()
            while True:
                chunk = response.read(8192)
                if not chunk:
                    break
                handler.wfile.write(chunk)
                handler.wfile.flush()

    def models(self) -> dict:
        data = []
        for platform in self._enabled_platforms():
            try:
                result = self.managers[platform].request("/v1/models", timeout=30)
                for item in result.get("data", []):
                    if isinstance(item, dict):
                        data.append({**item, "cli_platform": platform})
            except Exception:
                continue
        return {"object": "list", "data": data}

    def health(self) -> dict:
        return {"status": "ok" if self._server and not self._error else "degraded", "service": "heysure-unified-cli-gateway", "platforms": {name: manager.status() for name, manager in self.managers.items()}, "error": self._error or None}

    def request(self, path: str, payload: Optional[dict] = None, timeout: Optional[int] = None, headers: Optional[dict] = None) -> dict:
        if path.rstrip("/") == "/v1/models":
            return self.models()
        if path.rstrip("/") == "/v1/chat/completions":
            return self.complete(payload or {}, headers=headers)
        if path.rstrip("/") in ("", "/health"):
            return self.health()
        raise RuntimeError(f"统一网关不支持路径: {path}")

    def status(self) -> dict:
        platforms = {name: manager.status() for name, manager in self.managers.items()}
        logs = []
        for name, state in platforms.items():
            logs.extend(f"[{name}] {line}" for line in state.get("recentLogs", []))
        return {"ready": bool(self._server) and any(state.get("ready") for state in platforms.values()) and not self._error, "platform": "unified", "model": "model-routed", "enabled": bool(self._config.get("enabled")), "configured": True, "error": self._error or None, "recentLogs": logs[-30:], "listenHost": self._config.get("gatewayHost", "127.0.0.1"), "listenPort": self._config.get("gatewayPort", 8140), "exposeEnabled": self._config.get("gatewayHost") not in (None, "", "127.0.0.1", "localhost", "::1"), "platforms": platforms}

    def restart(self) -> None:
        config = self._config
        self.stop()
        self.apply(config)

    def stop(self) -> None:
        self._stop_api()
        for manager in self.managers.values():
            manager.stop()

    def _child_env(self, config: dict) -> dict:
        platform = str(config.get("platform") or self._config.get("defaultPlatform") or "codex")
        profile = dict(self._config.get("profiles", {}).get(platform) or {})
        if not profile:
            profile.update(config)
        profile["platform"] = platform
        profile["gatewayApiKey"] = ""
        return self.managers[platform]._child_env(profile)


class ManagementJob:
    """One interactive install/login/dependency job controlled by the web UI."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._process: Optional[subprocess.Popen[str]] = None
        self._logs: deque[str] = deque(maxlen=300)
        self._action = ""
        self._started_at = 0.0

    def _command(self, platform: str, action: str, config: dict) -> List[str]:
        command = str(config.get("command") or DEFAULT_COMMANDS[platform])
        if os.name != "nt":
            script = BASE_DIR / f"{platform}_cli_api" / "run.sh"
            return ["bash", str(script), action]
        if action == "deps":
            return [sys.executable, "-m", "pip", "install", "-r", str(BASE_DIR / "requirements.txt")]
        if action == "install-cli":
            if platform == "codex":
                return ["npm.cmd", "install", "-g", "@openai/codex"]
            installer = "https://x.ai/cli/install.ps1" if platform == "grok" else "https://antigravity.google/cli/install.ps1"
            return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", f"irm {installer} | iex"]
        if action == "login-status":
            if platform == "codex":
                return [command, "login", "status"]
            if platform == "antigravity":
                return [command, "models"]
            return [command, "--version"]
        if action == "login":
            return [command, "login"] if platform in {"codex", "grok"} else [command]
        raise ValueError(f"不支持的管理作业: {action}")

    def _reader(self, process: subprocess.Popen[str]) -> None:
        if process.stdout is None:
            return
        for line in process.stdout:
            clean = line.rstrip()
            if clean:
                self._logs.append(clean[-2000:])

    def start(self, platform: str, action: str, config: dict, env: dict) -> dict:
        with self._lock:
            if self._process and self._process.poll() is None:
                raise RuntimeError(f"已有作业正在运行: {self._action}")
            argv = self._command(platform, action, config)
            self._logs.clear()
            self._logs.append("$ " + " ".join(argv))
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            self._process = subprocess.Popen(
                argv, cwd=str(BASE_DIR / f"{platform}_cli_api"), env=env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                creationflags=flags,
            )
            self._action = action
            self._started_at = time.time()
            threading.Thread(target=self._reader, args=(self._process,), daemon=True).start()
        return self.status()

    def input(self, value: str) -> dict:
        with self._lock:
            if not self._process or self._process.poll() is not None or self._process.stdin is None:
                raise RuntimeError("当前没有等待输入的作业")
            self._process.stdin.write(value + "\n")
            self._process.stdin.flush()
        return self.status()

    def stop(self) -> dict:
        with self._lock:
            process = self._process
        if process and process.poll() is None:
            process.terminate()
        return self.status()

    def status(self) -> dict:
        with self._lock:
            running = bool(self._process and self._process.poll() is None)
            return {
                "running": running, "action": self._action,
                "exitCode": None if running or not self._process else self._process.returncode,
                "startedAt": self._started_at or None, "logs": list(self._logs),
            }


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
        self.connection_state = "idle"
        self.connection_error = ""
        self.last_login_at = 0.0
        self.gateway = UnifiedGatewayFleet()
        self.sio = socketio.Client(reconnection=True, reconnection_delay=2, logger=False)
        self._connection_lock = threading.RLock()
        self._connection_key: tuple[str, ...] = ()
        self._bind_events()
        self.apply_config(config)

    def login(self) -> None:
        self.connection_state = "logging-in"
        self.connection_error = ""
        data = _json_request(
            f"{self.server}/api/auth/login",
            {"account": self.account, "password": self.password}, timeout=15,
        )
        self.token = str(data.get("access_token") or "")
        if not self.token:
            raise RuntimeError("登录响应缺少 access_token")
        self.socket_url = str(data.get("agent_socket_url") or self.server).rstrip("/")
        self.last_login_at = time.time()

    def verify_login(self) -> dict:
        if not self.server or not self.account or not self.password:
            raise ValueError("请填写 HeySure 服务器地址、账号和密码")
        try:
            self.login()
        except Exception as exc:
            self.connection_state = "error"
            self.connection_error = str(exc)
            raise
        return {
            "authenticated": True,
            "server": self.server,
            "socketUrl": self.socket_url,
            "connected": self.sio.connected,
            "registered": self.registered,
        }

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
            self.connection_state = "connected"
            self.connection_error = ""
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
            self.connection_state = "registered"
            self.connection_error = ""
            self.registered = True
            print(f"已注册 {self.device_id}，绑定 AI: {data.get('aiConfigId') or '未绑定'}", flush=True)

        @self.sio.on("device:register_rejected")
        def rejected(data: dict) -> None:
            self.connection_state = "rejected"
            self.connection_error = str(data.get("reason") or "设备注册被拒绝")
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
        self.server = _server_url(data.get("server"))
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
            self.connection_state = "connecting" if self.account and self.password else "idle"
            self.connection_error = ""
        if self.server and self.account and self.password:
            threading.Thread(target=self._connect, daemon=True).start()

    def _connect(self) -> None:
        try:
            self.login()
            self.sio.connect(self.socket_url, transports=["websocket", "polling"], wait_timeout=15)
        except Exception as exc:
            self.connection_state = "error"
            self.connection_error = str(exc)
            print(f"连接 HeySure 失败: {exc}", flush=True)

    def _execute(self, tool: str, args: dict, task: dict) -> tuple[Any, str]:
        if tool == "cli.status":
            result = self.gateway.status()
            return result, "CLI Adapter 已就绪" if result["ready"] else f"CLI Adapter 未就绪：{result.get('error')}"
        if tool == "cli.models":
            result = self.gateway.request("/v1/models", timeout=30)
            models = [
                {"id": str(item.get("id")), "platform": item.get("cli_platform")}
                for item in result.get("data", []) if isinstance(item, dict) and item.get("id")
            ]
            return {"models": models, "gateway": "unified"}, f"发现 {len(models)} 个模型"
        if tool != "cli.run":
            raise ValueError(f"unknown tool: {tool}")
        prompt = str(args.get("prompt") or "").strip()
        if not prompt:
            raise ValueError("prompt 不能为空")
        model = str(args.get("model") or "").strip()
        selected_platform = str(args.get("platform") or "").strip().lower()
        session_id = str(args.get("session_id") or task.get("sessionId") or task.get("taskId") or "")
        timeout = max(30, min(int(args.get("timeout_seconds") or 300), 900))
        payload = {"messages": [{"role": "user", "content": prompt}], "stream": False, "user": session_id}
        if model:
            payload["model"] = model
        if selected_platform:
            payload["cli_platform"] = selected_platform
        response = self.gateway.request(
            "/v1/chat/completions", payload,
            timeout=timeout,
            headers={
                "X-HeySure-Session-ID": session_id,
                "X-HeySure-History-Mode": "incremental",
                "X-HeySure-Request-ID": str(task.get("taskId") or ""),
            },
        )
        choices = response.get("choices") or []
        message = choices[0].get("message") if choices and isinstance(choices[0], dict) else {}
        content = message.get("content") if isinstance(message, dict) else None
        if content is None:
            content = json.dumps(message or response, ensure_ascii=False)
        return {"content": content, "model": response.get("model"), "platform": response.get("cli_platform"), "usage": response.get("usage")}, str(content)[:500]

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
            "heysureConnectionState": self.connection_state,
            "heysureError": self.connection_error or None,
            "heysureLastLoginAt": self.last_login_at or None,
            "registered": self.registered,
            "deviceId": self.device_id,
            "gateway": self.gateway.status(),
        }


def _default_device_id() -> str:
    hostname = re.sub(r"[^a-zA-Z0-9_.-]+", "-", socket.gethostname()).strip("-") or "local"
    return f"heysure-cli-{hostname.lower()}"


def _env_number(name: str, default: Any, converter: Any = int) -> Any:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return converter(raw)
    except (TypeError, ValueError):
        print(f"忽略无效环境变量 {name}={raw!r}，使用默认值 {default}", flush=True)
        return default


def _default_config() -> dict:
    profiles = {
        "codex": {
            "platformEnabled": True,
            "command": os.getenv("CODEX_CLI_COMMAND", ""), "models": os.getenv("CODEX_CLI_MODELS", ""),
            "model": "", "workspace": "", "sessionsDir": os.getenv("CODEX_CLI_SESSIONS_DIR", ""),
            "timeoutSeconds": _env_number("CODEX_CLI_TIMEOUT", 900), "sandbox": os.getenv("CODEX_CLI_SANDBOX", "read-only"),
            "proxyUrl": "", "noProxy": "localhost,127.0.0.1,::1",
        },
        "grok": {
            "platformEnabled": False,
            "command": os.getenv("GROK_CLI_COMMAND", ""), "models": os.getenv("GROK_CLI_MODELS", "grok-4.5"),
            "model": "grok-4.5", "workspace": os.getenv("GROK_CLI_CWD", ""), "sessionsDir": "",
            "timeoutSeconds": _env_number("GROK_CLI_TIMEOUT", 600),
            "acpEnabled": os.getenv("GROK_CLI_ACP", "1").lower() not in {"0", "false", "no"},
            "toolGraceSeconds": _env_number("GROK_CLI_TOOL_GRACE", 0.5, float),
            "sessionTtlSeconds": _env_number("GROK_CLI_SESSION_TTL", 1800),
            "maxSessions": _env_number("GROK_CLI_MAX_SESSIONS", 6),
            "xaiApiKey": os.getenv("XAI_API_KEY", ""), "proxyUrl": "", "noProxy": "localhost,127.0.0.1,::1",
        },
        "antigravity": {
            "platformEnabled": False,
            "command": os.getenv("ANTIGRAVITY_CLI_COMMAND", ""), "models": os.getenv("ANTIGRAVITY_MODELS", "gemini-3.5-flash-medium,gemini-3.5-flash-high,gemini-3.5-flash-low,gemini-3.1-pro-low,gemini-3.1-pro-high"),
            "model": "gemini-3.5-flash-medium", "workspace": "", "sessionsDir": os.getenv("ANTIGRAVITY_CLI_SESSIONS_DIR", ""),
            "timeoutSeconds": _env_number("ANTIGRAVITY_TIMEOUT", 600),
            "backend": os.getenv("ANTIGRAVITY_BACKEND", "cli"), "argSafeBytes": _env_number("ANTIGRAVITY_CLI_ARG_SAFE_BYTES", 98304),
            "authFile": os.getenv("ANTIGRAVITY_AUTH_FILE", ""), "callbackPort": _env_number("ANTIGRAVITY_OAUTH_CALLBACK_PORT", 51121),
            "oauthClientId": os.getenv("ANTIGRAVITY_OAUTH_CLIENT_ID", ""), "oauthClientSecret": os.getenv("ANTIGRAVITY_OAUTH_CLIENT_SECRET", ""),
            "baseUrls": os.getenv("ANTIGRAVITY_BASE_URLS", ""), "proxyUrl": "", "noProxy": "localhost,127.0.0.1,::1",
        },
    }
    result = {
        "server": os.getenv("HEYSURE_SERVER") or _configured_server_url(),
        "account": os.getenv("HEYSURE_ACCOUNT", ""),
        "password": os.getenv("HEYSURE_PASSWORD", ""),
        "deviceId": os.getenv("HEYSURE_CLI_DEVICE_ID", _default_device_id()),
        "name": os.getenv("HEYSURE_CLI_DEVICE_NAME", "本机 CLI Adapter"),
        "platform": "codex",
        "profiles": profiles,
        "enabled": False,
        "gatewayHost": os.getenv("HEYSURE_CLI_GATEWAY_HOST", "127.0.0.1"),
        "gatewayPort": _env_number("HEYSURE_CLI_GATEWAY_PORT", 8140),
        "gatewayApiKey": os.getenv("HEYSURE_CLI_GATEWAY_API_KEY", ""),
        "gatewayPublicHost": os.getenv("HEYSURE_CLI_PUBLIC_HOST", ""),
        "defaultPlatform": os.getenv("HEYSURE_CLI_DEFAULT_PLATFORM", "codex"),
    }
    result.update(profiles["codex"])
    return result


def _bounded_number(
    raw: dict, previous: dict, name: str, default: Any,
    minimum: Any, maximum: Any, converter: Any = int,
) -> Any:
    value = raw.get(name)
    if value is None or (isinstance(value, str) and not value.strip()):
        value = previous.get(name, default)
    if value is None or (isinstance(value, str) and not value.strip()):
        value = default
    labels = {
        "timeoutSeconds": "超时秒数", "toolGraceSeconds": "工具收集窗口",
        "sessionTtlSeconds": "会话 TTL", "maxSessions": "最大会话数",
        "argSafeBytes": "参数安全字节数", "callbackPort": "OAuth 回调端口",
        "gatewayPort": "统一网关端口",
    }
    try:
        parsed = converter(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{labels.get(name, name)}必须是有效数字") from exc
    return max(minimum, min(parsed, maximum))


def _normalized_profile(platform: str, raw: dict, previous: dict) -> dict:
    def secret(name: str) -> str:
        if raw.get(f"{name}Clear") is True:
            return ""
        return str(raw.get(name) or previous.get(name) or "")
    result = {
        "platformEnabled": bool(raw.get("platformEnabled", previous.get("platformEnabled", platform == "codex"))),
        "command": str(raw.get("command", previous.get("command", ""))).strip(),
        "models": str(raw.get("models", previous.get("models", ""))).strip(),
        "model": str(raw.get("model", previous.get("model", ""))).strip(),
        "workspace": str(raw.get("workspace", previous.get("workspace", ""))).strip(),
        "sessionsDir": str(raw.get("sessionsDir", previous.get("sessionsDir", ""))).strip(),
        "timeoutSeconds": _bounded_number(raw, previous, "timeoutSeconds", 600, 30, 900),
        "proxyUrl": secret("proxyUrl").strip(),
        "noProxy": str(raw.get("noProxy", previous.get("noProxy", "localhost,127.0.0.1,::1"))).strip(),
    }
    if platform == "codex":
        sandbox = str(raw.get("sandbox") or previous.get("sandbox") or "read-only").strip().lower()
        if sandbox not in {"read-only", "workspace-write", "danger-full-access"}:
            raise ValueError("无效的 Codex sandbox")
        result["sandbox"] = sandbox
    elif platform == "grok":
        result.update({
            "acpEnabled": bool(raw.get("acpEnabled", previous.get("acpEnabled", True))),
            "toolGraceSeconds": _bounded_number(raw, previous, "toolGraceSeconds", 0.5, 0.05, 10.0, float),
            "sessionTtlSeconds": _bounded_number(raw, previous, "sessionTtlSeconds", 1800, 60, 86400),
            "maxSessions": _bounded_number(raw, previous, "maxSessions", 6, 1, 100),
            "xaiApiKey": secret("xaiApiKey"),
        })
    else:
        backend = str(raw.get("backend") or previous.get("backend") or "cli").strip().lower()
        if backend not in {"cli", "direct"}:
            raise ValueError("Antigravity backend 仅支持 cli/direct")
        result.update({
            "backend": backend,
            "argSafeBytes": _bounded_number(raw, previous, "argSafeBytes", 98304, 8192, 1048576),
            "authFile": str(raw.get("authFile", previous.get("authFile", ""))).strip(),
            "callbackPort": _bounded_number(raw, previous, "callbackPort", 51121, 1024, 65535),
            "oauthClientId": str(raw.get("oauthClientId", previous.get("oauthClientId", ""))).strip(),
            "oauthClientSecret": secret("oauthClientSecret"),
            "baseUrls": str(raw.get("baseUrls", previous.get("baseUrls", ""))).strip(),
        })
    return result


def _normalize_config(raw: dict, previous: Optional[dict] = None) -> dict:
    defaults = _default_config()
    previous = previous or defaults
    platform = str(raw.get("platform", previous.get("platform", "codex"))).strip().lower()
    if platform not in PLATFORMS:
        raise ValueError("platform 仅支持 codex、grok、antigravity")
    password = str(raw.get("password") or "")
    if not password:
        password = str(previous.get("password") or "")
    gateway_api_key = "" if raw.get("gatewayApiKeyClear") is True else str(raw.get("gatewayApiKey") or previous.get("gatewayApiKey") or "")
    gateway_host = str(raw.get("gatewayHost", previous.get("gatewayHost", "127.0.0.1"))).strip() or "127.0.0.1"
    gateway_port = _bounded_number(raw, previous, "gatewayPort", 8140, 1024, 65535)
    gateway_public_host = str(raw.get("gatewayPublicHost", previous.get("gatewayPublicHost", ""))).strip()
    if gateway_public_host and not re.fullmatch(r"[A-Za-z0-9._:\-\[\]]+", gateway_public_host):
        raise ValueError("公网访问地址只填写域名或 IP，不要包含协议、端口或路径")
    default_platform = str(raw.get("defaultPlatform", previous.get("defaultPlatform", "codex"))).strip().lower()
    if default_platform not in PLATFORMS:
        raise ValueError("默认平台仅支持 codex、grok、antigravity")
    if gateway_host not in {"127.0.0.1", "localhost", "::1"} and not gateway_api_key:
        raise ValueError("统一网关对外监听时必须设置 API Key")
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
        "server": _server_url(raw.get("server", previous.get("server", ""))),
        "account": str(raw.get("account", previous.get("account", ""))).strip(),
        "password": password,
        "deviceId": str(raw.get("deviceId", previous.get("deviceId", _default_device_id()))).strip(),
        "name": str(raw.get("name", previous.get("name", "本机 CLI Adapter"))).strip(),
        "platform": platform,
        "profiles": profiles,
        "enabled": bool(raw.get("enabled", previous.get("enabled", True))),
        "gatewayHost": gateway_host,
        "gatewayPort": gateway_port,
        "gatewayApiKey": gateway_api_key,
        "gatewayPublicHost": gateway_public_host,
        "defaultPlatform": default_platform,
    }
    result.update(profiles[platform])
    if not result["server"] or not result["deviceId"]:
        raise ValueError("HeySure 地址和设备 ID 不能为空")
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


def _autostart(action: str) -> dict:
    """Manage autostart for the unified adapter, never the legacy child gateways."""
    if os.name == "nt":
        task_name = "HeySureCLIAdapter"
        if action == "status":
            completed = subprocess.run(["schtasks", "/Query", "/TN", task_name], capture_output=True, text=True, errors="replace", check=False)
            return {"enabled": completed.returncode == 0, "output": (completed.stdout or completed.stderr).strip()[-4000:]}
        if action == "on":
            launch = f'"{sys.executable}" "{BASE_DIR / "agent.py"}"'
            argv = ["schtasks", "/Create", "/SC", "ONLOGON", "/TN", task_name, "/TR", launch, "/F"]
        elif action == "off":
            argv = ["schtasks", "/Delete", "/TN", task_name, "/F"]
        else:
            raise ValueError("autostart 仅支持 on/off/status")
        completed = subprocess.run(argv, capture_output=True, text=True, errors="replace", check=False)
        if completed.returncode != 0:
            raise RuntimeError((completed.stderr or completed.stdout or "开机自启操作失败").strip())
        return _autostart("status")
    if sys.platform.startswith("linux"):
        unit_dir = Path.home() / ".config" / "systemd" / "user"
        unit_path = unit_dir / "heysure-cli-adapter.service"
        if action == "status":
            completed = subprocess.run(["systemctl", "--user", "is-enabled", "heysure-cli-adapter.service"], capture_output=True, text=True, check=False)
            return {"enabled": completed.returncode == 0, "output": (completed.stdout or completed.stderr).strip()}
        if action == "on":
            unit_dir.mkdir(parents=True, exist_ok=True)
            unit_path.write_text(
                "[Unit]\nDescription=HeySure CLI Adapter\nAfter=network-online.target\n\n"
                "[Service]\nType=simple\nRestart=always\nRestartSec=3\n"
                f"WorkingDirectory={BASE_DIR}\nExecStart={sys.executable} {BASE_DIR / 'agent.py'}\n\n"
                "[Install]\nWantedBy=default.target\n", encoding="utf-8",
            )
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
            subprocess.run(["systemctl", "--user", "enable", "--now", "heysure-cli-adapter.service"], check=True)
        elif action == "off":
            subprocess.run(["systemctl", "--user", "disable", "--now", "heysure-cli-adapter.service"], check=False)
        else:
            raise ValueError("autostart 仅支持 on/off/status")
        return _autostart("status")
    raise RuntimeError("当前系统暂不支持自动配置开机自启")


class ControlApp:
    def __init__(self, config: dict) -> None:
        self._lock = threading.RLock()
        self.config = config
        self.adapter = CliAdapter(config)
        self.job = ManagementJob()

    def public_config(self) -> dict:
        with self._lock:
            value = json.loads(json.dumps(self.config))
            value["passwordConfigured"] = bool(value.get("password"))
            value["password"] = ""
            value["gatewayApiKeyConfigured"] = bool(value.get("gatewayApiKey"))
            value["gatewayApiKey"] = ""
            for profile in value.get("profiles", {}).values():
                for key in ("xaiApiKey", "oauthClientSecret"):
                    if key in profile:
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
        return {**self.adapter.status(), "job": self.job.status()}

    def detect_cli_config(self, platform: str, raw_profile: Optional[dict] = None) -> dict:
        if platform not in PLATFORMS:
            raise ValueError("platform 仅支持 codex、grok、antigravity")
        profile = dict(self.config.get("profiles", {}).get(platform) or {})
        if isinstance(raw_profile, dict):
            for key in ("command", "model", "models", "workspace", "sessionsDir"):
                if key in raw_profile:
                    profile[key] = raw_profile[key]
        requested_command = str(profile.get("command") or DEFAULT_COMMANDS[platform]).strip()
        expanded = Path(requested_command).expanduser()
        resolved_command = str(expanded.resolve()) if expanded.is_file() else (shutil.which(requested_command) or "")
        warnings: List[str] = []
        version = ""
        if resolved_command:
            try:
                completed = subprocess.run(
                    [resolved_command, "--version"], cwd=str(BASE_DIR),
                    env=self.adapter.gateway._child_env({"platform": platform}),
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                    timeout=12, check=False,
                )
                version = (completed.stdout or completed.stderr or "").strip()[-1000:]
            except (OSError, subprocess.TimeoutExpired) as exc:
                warnings.append(f"版本探测失败: {exc}")
        else:
            warnings.append(f"未找到 {requested_command}，请先安装 CLI 或手动填写路径")

        models: List[str] = []
        model_source = ""
        manager = self.adapter.gateway.managers[platform]
        if manager.status().get("ready"):
            try:
                response = manager.request("/v1/models", timeout=30)
                models = [
                    str(item.get("id")).strip() for item in response.get("data", [])
                    if isinstance(item, dict) and item.get("id")
                ]
                model_source = "运行中的 CLI 后端"
            except RuntimeError as exc:
                warnings.append(f"后端模型探测失败: {exc}")
        if not models and platform == "codex" and resolved_command:
            try:
                completed = subprocess.run(
                    [resolved_command, "debug", "models"], cwd=str(BASE_DIR),
                    env=self.adapter.gateway._child_env({"platform": platform}),
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                    timeout=30, check=False,
                )
                payload = json.loads(completed.stdout) if completed.returncode == 0 else {}
                entries = payload.get("models") if isinstance(payload, dict) else []
                visible = [
                    (int(item.get("priority") or 999999), str(item.get("slug") or "").strip())
                    for item in entries if isinstance(item, dict) and item.get("visibility") != "hide" and item.get("slug")
                ]
                models = [slug for _, slug in sorted(visible)]
                model_source = "Codex CLI"
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
        if not models:
            models = [item.strip() for item in str(profile.get("models") or "").split(",") if item.strip()]
            model_source = "平台推荐值" if models else "未识别"
        models = list(dict.fromkeys(models))
        defaults = _default_config()["profiles"][platform]
        current_model = str(profile.get("model") or "").strip()
        fallback_models = {"codex": "codex-default", "grok": "grok-4.5", "antigravity": "gemini-3.5-flash-medium"}
        default_model = current_model if current_model in models else (models[0] if models else str(defaults.get("model") or fallback_models[platform]))
        workspace = str(profile.get("workspace") or "").strip()
        if not workspace or not Path(workspace).expanduser().is_dir():
            workspace = str(BASE_DIR)
        sessions_dir = str(profile.get("sessionsDir") or "").strip()
        if not sessions_dir:
            sessions_dir = str(CONTROL_RUNTIME_DIR / "sessions" / platform)
        return {
            "platform": platform,
            "values": {
                "command": resolved_command or requested_command,
                "model": default_model,
                "timeoutSeconds": int(defaults["timeoutSeconds"]),
                "models": ",".join(models),
                "workspace": workspace,
                "sessionsDir": sessions_dir,
            },
            "detected": {
                "commandFound": bool(resolved_command),
                "version": version,
                "modelSource": model_source,
                "modelCount": len(models),
            },
            "warnings": warnings,
        }

    def action(self, action: str, payload: dict) -> dict:
        if action == "generate-api-key":
            return {"apiKey": "hs_" + secrets.token_urlsafe(32)}
        if action == "heysure-login":
            # Saving the HeySure section already schedules the Socket connection.
            # This action verifies the HTTP login synchronously so the UI can show
            # credential/server errors instead of silently waiting on that thread.
            return self.adapter.verify_login()
        if action == "detect-cli-config":
            platform = str(payload.get("platform") or self.config.get("platform") or "codex").strip().lower()
            return self.detect_cli_config(platform, payload.get("profile"))
        if action in {"deps", "install-cli", "login", "login-status"}:
            return self.job.start(
                self.config["platform"], action, self.config,
                self.adapter.gateway._child_env(self.config),
            )
        if action == "job-input":
            return self.job.input(str(payload.get("input") or ""))
        if action == "job-stop":
            return self.job.stop()
        if action == "job-status":
            return self.job.status()
        if action == "start":
            self.config["enabled"] = True
            _save_config(self.config)
            self.adapter.apply_config(self.config)
            return self.adapter.gateway.status()
        if action == "stop":
            self.config["enabled"] = False
            _save_config(self.config)
            self.adapter.apply_config(self.config)
            return self.adapter.gateway.status()
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
        if action == "status":
            return self.status()
        if action == "logs":
            state = self.adapter.gateway.status()
            return {"gateway": "unified", "logs": state.get("recentLogs", []), "platforms": state.get("platforms", {})}
        if action == "proxy-test":
            proxy = str(self.config.get("proxyUrl") or "").strip()
            if not proxy:
                raise ValueError("当前平台未配置代理 URL")
            opener = build_opener(ProxyHandler({"http": proxy, "https": proxy}))
            with opener.open("https://www.google.com/generate_204", timeout=15) as response:
                return {"ok": response.status in {200, 204}, "status": response.status}
        if action == "autostart":
            return _autostart(str(payload.get("mode") or "status"))
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
    parser.add_argument("--host", default=os.getenv("HEYSURE_CLI_CONTROL_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=_env_number("HEYSURE_CLI_CONTROL_PORT", 8130))
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
