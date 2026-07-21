"""Antigravity OAuth to OpenAI-compatible HTTP gateway.

This module intentionally uses only Python's standard library.  It implements
the OAuth and v1internal request flow used by Antigravity-compatible clients;
it does not start the Antigravity desktop application or Gemini CLI.
"""

from __future__ import annotations

import argparse
import base64
import copy
import datetime as dt
import hashlib
import json
import mimetypes
import os
import queue
import re
import secrets
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Iterable, List, Optional, Tuple


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RUNTIME_DIR = os.path.join(BASE_DIR, "runtime")
MAX_BODY_BYTES = 64 * 1024 * 1024
MAX_INLINE_BYTES = 20 * 1024 * 1024
FINGERPRINT = "antigravity-python-gateway"

DEFAULT_SCOPES = (
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/cclog",
    "https://www.googleapis.com/auth/experimentsandconfigs",
)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)) or default)
    except ValueError:
        return default


def _env_list(name: str, default: str) -> List[str]:
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]


class Config:
    host = os.environ.get("ANTIGRAVITY_HOST", "127.0.0.1")
    port = _env_int("ANTIGRAVITY_PORT", 8110)
    timeout = _env_int("ANTIGRAVITY_TIMEOUT", 600)
    api_key = os.environ.get("ANTIGRAVITY_API_KEY", "").strip()
    auth_file = os.path.abspath(
        os.path.expanduser(
            os.environ.get(
                "ANTIGRAVITY_AUTH_FILE",
                os.path.join(RUNTIME_DIR, "antigravity-auth.json"),
            )
        )
    )
    models = _env_list(
        "ANTIGRAVITY_MODELS",
        "gemini-pro-agent,gemini-3.1-pro-low,gemini-3.5-flash-low,gemini-3.1-flash-lite",
    )
    client_id = os.environ.get("ANTIGRAVITY_OAUTH_CLIENT_ID", "").strip()
    client_secret = os.environ.get("ANTIGRAVITY_OAUTH_CLIENT_SECRET", "").strip()
    callback_port = _env_int("ANTIGRAVITY_OAUTH_CALLBACK_PORT", 51121)
    token_endpoint = os.environ.get(
        "ANTIGRAVITY_TOKEN_ENDPOINT", "https://oauth2.googleapis.com/token"
    ).strip()
    auth_endpoint = os.environ.get(
        "ANTIGRAVITY_AUTH_ENDPOINT", "https://accounts.google.com/o/oauth2/v2/auth"
    ).strip()
    userinfo_endpoint = os.environ.get(
        "ANTIGRAVITY_USERINFO_ENDPOINT",
        "https://www.googleapis.com/oauth2/v2/userinfo?alt=json",
    ).strip()
    base_urls = _env_list(
        "ANTIGRAVITY_BASE_URLS",
        "https://daily-cloudcode-pa.googleapis.com,https://cloudcode-pa.googleapis.com",
    )
    user_agent = os.environ.get("ANTIGRAVITY_USER_AGENT", "").strip()
    version_manifest = os.environ.get(
        "ANTIGRAVITY_VERSION_MANIFEST",
        "https://antigravity-hub-auto-updater-974169037036.us-central1.run.app/manifest/latest-arm64-mac.yml",
    ).strip()


class GatewayError(Exception):
    def __init__(self, message: str, status: int = 502, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


def _safe_error_body(data: bytes) -> str:
    text = data[:8192].decode("utf-8", errors="replace").strip()
    for key in ("access_token", "refresh_token", "id_token"):
        text = re.sub(
            rf'("{key}"\s*:\s*")[^"]+("?)', rf"\1[redacted]\2", text, flags=re.I
        )
    return text


def _urlopen(request: urllib.request.Request, timeout: Optional[int] = None):
    try:
        return urllib.request.urlopen(request, timeout=timeout or Config.timeout)
    except urllib.error.HTTPError as exc:
        body = exc.read(8192)
        raise GatewayError(
            f"上游请求失败：HTTP {exc.code}", status=exc.code, body=_safe_error_body(body)
        ) from exc
    except urllib.error.URLError as exc:
        raise GatewayError(f"无法连接上游：{exc.reason}", status=502) from exc


def _request_json(
    url: str,
    payload: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    method: Optional[str] = None,
    timeout: Optional[int] = None,
) -> Dict[str, Any]:
    data = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
    request = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"))
    if data is not None:
        request.add_header("Content-Type", "application/json")
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    with _urlopen(request, timeout) as response:
        raw = response.read(MAX_BODY_BYTES + 1)
    if len(raw) > MAX_BODY_BYTES:
        raise GatewayError("上游响应过大", status=502)
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GatewayError("上游返回的不是有效 JSON", status=502) from exc
    if not isinstance(decoded, dict):
        raise GatewayError("上游 JSON 顶层不是对象", status=502)
    return decoded


def _request_form(url: str, values: Dict[str, str]) -> Dict[str, Any]:
    encoded = urllib.parse.urlencode(values).encode("utf-8")
    request = urllib.request.Request(url, data=encoded, method="POST")
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    with _urlopen(request, 30) as response:
        raw = response.read(1024 * 1024)
    try:
        result = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GatewayError("OAuth 服务返回了无效 JSON", status=502) from exc
    if not isinstance(result, dict):
        raise GatewayError("OAuth 服务返回格式错误", status=502)
    return result


_user_agent_lock = threading.Lock()
_cached_user_agent = ""


def antigravity_user_agent() -> str:
    global _cached_user_agent
    if Config.user_agent:
        return Config.user_agent
    with _user_agent_lock:
        if _cached_user_agent:
            return _cached_user_agent
        version = "2.2.1"
        if Config.version_manifest:
            try:
                request = urllib.request.Request(
                    Config.version_manifest,
                    headers={"User-Agent": "electron-builder", "Cache-Control": "no-cache"},
                )
                with _urlopen(request, 10) as response:
                    manifest = response.read(64 * 1024).decode("utf-8", errors="replace")
                match = re.search(r"(?m)^version:\s*['\"]?([^\s'\"]+)", manifest)
                if match:
                    version = match.group(1)
            except Exception:
                pass
        _cached_user_agent = f"antigravity/hub/{version} darwin/arm64"
        return _cached_user_agent


def _iso_expiry(seconds: int) -> str:
    value = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=max(0, seconds))
    return value.isoformat().replace("+00:00", "Z")


def _parse_expiry(record: Dict[str, Any]) -> float:
    raw = record.get("expired")
    if isinstance(raw, str) and raw:
        try:
            return dt.datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    try:
        timestamp_ms = float(record.get("timestamp") or 0)
        return timestamp_ms / 1000.0 + float(record.get("expires_in") or 0)
    except (TypeError, ValueError):
        return 0


def _extract_project(data: Dict[str, Any]) -> str:
    for key in ("cloudaicompanionProject", "projectId", "project"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            project_id = value.get("id")
            if isinstance(project_id, str) and project_id.strip():
                return project_id.strip()
    return ""


def _default_tier(data: Dict[str, Any]) -> str:
    for item in data.get("allowedTiers") or []:
        if isinstance(item, dict) and item.get("isDefault") and str(item.get("id") or "").strip():
            return str(item["id"]).strip()
    current = data.get("currentTier")
    if isinstance(current, dict) and str(current.get("id") or "").strip():
        return str(current["id"]).strip()
    return "free-tier"


def discover_project(access_token: str) -> str:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "*/*",
        "User-Agent": antigravity_user_agent(),
    }
    prod = Config.base_urls[-1] if Config.base_urls else "https://cloudcode-pa.googleapis.com"
    load = _request_json(
        prod.rstrip("/") + "/v1internal:loadCodeAssist",
        {"metadata": {"ideType": "ANTIGRAVITY"}},
        headers,
        timeout=30,
    )
    project = _extract_project(load)
    if project:
        return project

    tier = _default_tier(load)
    daily = Config.base_urls[0] if Config.base_urls else "https://daily-cloudcode-pa.googleapis.com"
    version_match = re.search(r"antigravity(?:/hub)?/([^\s]+)", antigravity_user_agent(), re.I)
    version = version_match.group(1) if version_match else "2.2.1"
    body = {
        "tier_id": tier,
        "metadata": {
            "ide_type": "ANTIGRAVITY",
            "ide_version": version,
            "ide_name": "antigravity",
        },
    }
    onboard_headers = dict(headers)
    onboard_headers["User-Agent"] = antigravity_user_agent() + " google-api-nodejs-client/10.3.0"
    onboard_headers["X-Goog-Api-Client"] = "gl-node/22.21.1"
    for _ in range(5):
        result = _request_json(
            daily.rstrip("/") + "/v1internal:onboardUser",
            body,
            onboard_headers,
            timeout=30,
        )
        if result.get("done"):
            response = result.get("response")
            project = _extract_project(response if isinstance(response, dict) else {})
            if project:
                return project
            raise GatewayError("Antigravity 初始化完成，但没有返回 project_id", status=502)
        time.sleep(2)
    raise GatewayError("Antigravity 初始化超时，未取得 project_id", status=504)


class TokenStore:
    def __init__(self, path: Optional[str] = None) -> None:
        self.path = os.path.abspath(os.path.expanduser(path or Config.auth_file))
        self.lock = threading.RLock()

    def load(self) -> Dict[str, Any]:
        with self.lock:
            try:
                with open(self.path, "r", encoding="utf-8") as handle:
                    value = json.load(handle)
            except FileNotFoundError as exc:
                raise GatewayError("尚未登录 Antigravity，请先运行 login", status=401) from exc
            except (OSError, json.JSONDecodeError) as exc:
                raise GatewayError(f"无法读取认证文件：{exc}", status=500) from exc
            if not isinstance(value, dict):
                raise GatewayError("认证文件格式错误", status=500)
            return value

    def save(self, record: Dict[str, Any]) -> None:
        with self.lock:
            parent = os.path.dirname(self.path)
            os.makedirs(parent, mode=0o700, exist_ok=True)
            fd, temp_path = tempfile.mkstemp(prefix="antigravity-auth.", suffix=".json", dir=parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(record, handle, ensure_ascii=False, indent=2)
                    handle.write("\n")
                os.chmod(temp_path, 0o600)
                os.replace(temp_path, self.path)
                try:
                    os.chmod(self.path, 0o600)
                except OSError:
                    pass
            finally:
                if os.path.exists(temp_path):
                    os.unlink(temp_path)

    def exists(self) -> bool:
        return os.path.isfile(self.path)

    def refresh(self, force: bool = False) -> Tuple[str, Dict[str, Any]]:
        with self.lock:
            record = self.load()
            token = str(record.get("access_token") or "").strip()
            if not force and token and _parse_expiry(record) > time.time() + 300:
                return token, record
            refresh_token = str(record.get("refresh_token") or "").strip()
            if not refresh_token:
                raise GatewayError("认证已过期且缺少 refresh_token，请重新登录", status=401)
            result = _request_form(
                Config.token_endpoint,
                {
                    "client_id": Config.client_id,
                    "client_secret": Config.client_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                },
            )
            access_token = str(result.get("access_token") or "").strip()
            if not access_token:
                raise GatewayError("OAuth 刷新响应缺少 access_token", status=401)
            expires_in = int(result.get("expires_in") or 3600)
            record.update(
                {
                    "type": "antigravity",
                    "access_token": access_token,
                    "expires_in": expires_in,
                    "timestamp": int(time.time() * 1000),
                    "expired": _iso_expiry(expires_in),
                }
            )
            if result.get("refresh_token"):
                record["refresh_token"] = result["refresh_token"]
            if not str(record.get("project_id") or "").strip():
                record["project_id"] = discover_project(access_token)
            self.save(record)
            return access_token, record


class _OAuthCallbackHandler(BaseHTTPRequestHandler):
    result_queue: "queue.Queue[Dict[str, str]]"

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/oauth-callback":
            self.send_error(404)
            return
        query = urllib.parse.parse_qs(parsed.query)
        result = {name: (query.get(name) or [""])[0].strip() for name in ("code", "state", "error")}
        self.result_queue.put(result)
        ok = bool(result["code"] and not result["error"])
        body = (
            "<h1>Login successful</h1><p>You can close this window.</p>"
            if ok
            else "<h1>Login failed</h1><p>Please check the terminal.</p>"
        ).encode()
        self.send_response(200 if ok else 400)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: Any) -> None:
        return


def _parse_callback(value: str) -> Optional[Dict[str, str]]:
    value = value.strip()
    if not value:
        return None
    parsed = urllib.parse.urlparse(value)
    query = urllib.parse.parse_qs(parsed.query)
    if not query and "=" in value:
        query = urllib.parse.parse_qs(value.lstrip("?"))
    return {name: (query.get(name) or [""])[0].strip() for name in ("code", "state", "error")}


def oauth_login(auth_file: str, callback_port: int, open_browser: bool) -> Dict[str, Any]:
    if not Config.client_id or not Config.client_secret:
        raise GatewayError(
            "未配置 ANTIGRAVITY_OAUTH_CLIENT_ID / ANTIGRAVITY_OAUTH_CLIENT_SECRET",
            status=500,
        )
    state = secrets.token_urlsafe(32)
    result_queue: "queue.Queue[Dict[str, str]]" = queue.Queue()
    handler = type("OAuthCallbackHandler", (_OAuthCallbackHandler,), {"result_queue": result_queue})
    try:
        callback_server = ThreadingHTTPServer(("127.0.0.1", callback_port), handler)
    except OSError as exc:
        raise GatewayError(f"无法监听 OAuth 回调端口 {callback_port}：{exc}", status=500) from exc
    actual_port = int(callback_server.server_address[1])
    redirect_uri = f"http://localhost:{actual_port}/oauth-callback"
    params = {
        "access_type": "offline",
        "client_id": Config.client_id,
        "prompt": "consent",
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(DEFAULT_SCOPES),
        "state": state,
    }
    auth_url = Config.auth_endpoint + "?" + urllib.parse.urlencode(params)
    thread = threading.Thread(target=callback_server.serve_forever, daemon=True)
    thread.start()

    print("\n请在本地浏览器打开以下 URL，使用已开通学生 AI Pro 的 Google 账号登录：\n")
    print(auth_url)
    print("\n远程服务器推荐先在本地电脑建立 SSH 隧道：")
    print(f"  ssh -L {actual_port}:127.0.0.1:{actual_port} <user>@<server>")
    print("也可以授权后复制浏览器地址栏里的完整 localhost 回调 URL，粘贴到这里。")
    if open_browser:
        try:
            webbrowser.open(auth_url)
        except Exception:
            pass

    def prompt_callback() -> None:
        try:
            raw = input("\n回调 URL（已使用 SSH 隧道时可等待浏览器自动完成）> ")
            parsed = _parse_callback(raw)
            if parsed is not None:
                result_queue.put(parsed)
        except (EOFError, KeyboardInterrupt):
            return

    threading.Thread(target=prompt_callback, daemon=True).start()
    try:
        result = result_queue.get(timeout=300)
    except queue.Empty as exc:
        raise GatewayError("OAuth 登录等待超时", status=504) from exc
    finally:
        callback_server.shutdown()
        callback_server.server_close()
        thread.join(timeout=2)

    if result.get("error"):
        raise GatewayError(f"Google 授权失败：{result['error']}", status=401)
    if not secrets.compare_digest(result.get("state") or "", state):
        raise GatewayError("OAuth state 不匹配，已拒绝该回调", status=401)
    code = result.get("code") or ""
    if not code:
        raise GatewayError("OAuth 回调缺少 authorization code", status=401)

    token = _request_form(
        Config.token_endpoint,
        {
            "code": code,
            "client_id": Config.client_id,
            "client_secret": Config.client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
    )
    access_token = str(token.get("access_token") or "").strip()
    refresh_token = str(token.get("refresh_token") or "").strip()
    if not access_token or not refresh_token:
        raise GatewayError("OAuth 响应缺少 access_token 或 refresh_token", status=401)
    user = _request_json(
        Config.userinfo_endpoint,
        headers={"Authorization": f"Bearer {access_token}", "User-Agent": antigravity_user_agent()},
        method="GET",
        timeout=30,
    )
    email = str(user.get("email") or "").strip()
    if not email:
        raise GatewayError("Google userinfo 未返回邮箱", status=502)
    project_id = discover_project(access_token)
    expires_in = int(token.get("expires_in") or 3600)
    record: Dict[str, Any] = {
        "type": "antigravity",
        "email": email,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_in": expires_in,
        "timestamp": int(time.time() * 1000),
        "expired": _iso_expiry(expires_in),
        "project_id": project_id,
    }
    TokenStore(auth_file).save(record)
    print(f"\nAntigravity 登录成功：{email}")
    print(f"项目：{project_id}")
    print(f"凭证：{os.path.abspath(auth_file)}（权限 600）")
    return record


def _sanitize_tool_names(tools: Any) -> Tuple[Dict[str, str], Dict[str, str]]:
    original_to_safe: Dict[str, str] = {}
    safe_to_original: Dict[str, str] = {}
    for item in tools or []:
        if not isinstance(item, dict):
            continue
        fn = item.get("function") if item.get("type", "function") == "function" else None
        if not isinstance(fn, dict):
            continue
        original = str(fn.get("name") or "").strip()
        if not original:
            continue
        base = re.sub(r"[^A-Za-z0-9_.:-]", "_", original)
        if not re.match(r"^[A-Za-z_]", base):
            base = "fn_" + base
        base = base[:64] or "function"
        safe = base
        counter = 2
        while safe in safe_to_original and safe_to_original[safe] != original:
            suffix = f"_{counter}"
            safe = base[: 64 - len(suffix)] + suffix
            counter += 1
        original_to_safe[original] = safe
        safe_to_original[safe] = original
    return original_to_safe, safe_to_original


def _json_result(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _download_data(url: str) -> Tuple[str, str]:
    request = urllib.request.Request(url, headers={"User-Agent": "HeySure-Antigravity/1.0"})
    with _urlopen(request, 20) as response:
        mime = str(response.headers.get_content_type() or "application/octet-stream")
        data = response.read(MAX_INLINE_BYTES + 1)
    if len(data) > MAX_INLINE_BYTES:
        raise GatewayError("图片或文件超过 20 MiB", status=400)
    return mime, base64.b64encode(data).decode("ascii")


def _inline_part(value: str, filename: str = "") -> Optional[Dict[str, Any]]:
    value = value.strip()
    match = re.match(r"^data:([^;,]+);base64,(.+)$", value, re.I | re.S)
    if match:
        encoded = re.sub(r"\s+", "", match.group(2))
        if len(encoded) > (MAX_INLINE_BYTES * 4 // 3) + 16:
            raise GatewayError("内联图片或文件超过 20 MiB", status=400)
        try:
            base64.b64decode(encoded, validate=True)
        except Exception as exc:
            raise GatewayError("内联 base64 数据无效", status=400) from exc
        return {"inlineData": {"mimeType": match.group(1), "data": encoded}}
    if value.startswith(("http://", "https://")):
        mime, encoded = _download_data(value)
        return {"inlineData": {"mimeType": mime, "data": encoded}}
    if value and filename:
        mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        try:
            base64.b64decode(value, validate=True)
        except Exception:
            return None
        return {"inlineData": {"mimeType": mime, "data": value}}
    return None


def _content_parts(content: Any) -> List[Dict[str, Any]]:
    if isinstance(content, str):
        return [{"text": content}] if content else []
    if content is None:
        return []
    if not isinstance(content, list):
        return [{"text": str(content)}]
    parts: List[Dict[str, Any]] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or "").lower()
        if kind in ("text", "input_text", "output_text"):
            text = str(item.get("text") or "")
            if text:
                parts.append({"text": text})
        elif kind in ("image_url", "input_image"):
            source = item.get("image_url") or item.get("url") or ""
            if isinstance(source, dict):
                source = source.get("url") or ""
            part = _inline_part(str(source))
            if part:
                part["thoughtSignature"] = "skip_thought_signature_validator"
                parts.append(part)
        elif kind in ("file", "input_file"):
            file_obj = item.get("file") if isinstance(item.get("file"), dict) else item
            filename = str(file_obj.get("filename") or "")
            source = str(file_obj.get("file_data") or file_obj.get("data") or "")
            part = _inline_part(source, filename)
            if part:
                parts.append(part)
    return parts


def _generation_config(payload: Dict[str, Any]) -> Dict[str, Any]:
    supplied = payload.get("generationConfig") or payload.get("generation_config")
    result = copy.deepcopy(supplied) if isinstance(supplied, dict) else {}
    mappings = {
        "temperature": "temperature",
        "top_p": "topP",
        "top_k": "topK",
        "presence_penalty": "presencePenalty",
        "frequency_penalty": "frequencyPenalty",
    }
    for source, target in mappings.items():
        if payload.get(source) is not None:
            result[target] = payload[source]
    stop = payload.get("stop")
    if isinstance(stop, str):
        result["stopSequences"] = [stop]
    elif isinstance(stop, list):
        result["stopSequences"] = [str(item) for item in stop]
    if isinstance(payload.get("n"), int) and payload["n"] > 1:
        result["candidateCount"] = payload["n"]
    effort = str(payload.get("reasoning_effort") or "").strip().lower()
    if effort:
        thinking: Dict[str, Any] = {"includeThoughts": effort != "none"}
        if effort == "auto":
            thinking["thinkingBudget"] = -1
        elif effort != "none":
            thinking["thinkingLevel"] = effort
        result["thinkingConfig"] = thinking
    response_format = payload.get("response_format")
    if isinstance(response_format, dict):
        if response_format.get("type") == "json_object":
            result["responseMimeType"] = "application/json"
        elif response_format.get("type") == "json_schema":
            schema = response_format.get("json_schema")
            if isinstance(schema, dict):
                result["responseMimeType"] = "application/json"
                result["responseJsonSchema"] = schema.get("schema") or schema
    return result


def openai_to_antigravity(
    payload: Dict[str, Any], project_id: str
) -> Tuple[str, Dict[str, Any], Dict[str, str]]:
    requested_model = str(payload.get("model") or "auto").strip()
    model = Config.models[0] if requested_model in ("", "auto") else requested_model
    original_to_safe, safe_to_original = _sanitize_tool_names(payload.get("tools"))
    system_parts: List[Dict[str, Any]] = []
    contents: List[Dict[str, Any]] = []
    tool_call_names: Dict[str, str] = {}

    for message in payload.get("messages") or []:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "user").lower()
        parts = _content_parts(message.get("content"))
        if role in ("system", "developer"):
            system_parts.extend(parts)
            continue
        if role == "assistant":
            reasoning = str(message.get("reasoning_content") or "")
            if reasoning:
                parts.insert(
                    0,
                    {
                        "text": reasoning,
                        "thought": True,
                        "thoughtSignature": "skip_thought_signature_validator",
                    },
                )
            for call in message.get("tool_calls") or []:
                if not isinstance(call, dict) or call.get("type", "function") != "function":
                    continue
                fn = call.get("function") or {}
                original_name = str(fn.get("name") or "").strip()
                if not original_name:
                    continue
                safe_name = original_to_safe.get(original_name, original_name)
                call_id = str(call.get("id") or f"call_{uuid.uuid4().hex}")
                tool_call_names[call_id] = safe_name
                arguments = fn.get("arguments") or "{}"
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except json.JSONDecodeError:
                        arguments = {"params": arguments}
                parts.append(
                    {
                        "functionCall": {"id": call_id, "name": safe_name, "args": arguments},
                        "thoughtSignature": "skip_thought_signature_validator",
                    }
                )
            if parts:
                contents.append({"role": "model", "parts": parts})
        elif role == "tool":
            call_id = str(message.get("tool_call_id") or "")
            original_name = str(message.get("name") or "")
            safe_name = tool_call_names.get(call_id) or original_to_safe.get(original_name, original_name)
            if not safe_name:
                safe_name = "tool"
            contents.append(
                {
                    "role": "user",
                    "parts": [
                        {
                            "functionResponse": {
                                "id": call_id,
                                "name": safe_name,
                                "response": {"result": _json_result(message.get("content"))},
                            }
                        }
                    ],
                }
            )
        else:
            if parts:
                contents.append({"role": "user", "parts": parts})

    if not contents:
        contents.append({"role": "user", "parts": [{"text": ""}]})
    request_body: Dict[str, Any] = {"contents": contents}
    if system_parts:
        request_body["systemInstruction"] = {"role": "user", "parts": system_parts}
    generation = _generation_config(payload)
    if generation:
        request_body["generationConfig"] = generation

    declarations: List[Dict[str, Any]] = []
    for tool in payload.get("tools") or []:
        if not isinstance(tool, dict) or tool.get("type", "function") != "function":
            continue
        fn = tool.get("function")
        if not isinstance(fn, dict) or not str(fn.get("name") or "").strip():
            continue
        original_name = str(fn["name"]).strip()
        declaration = {
            "name": original_to_safe.get(original_name, original_name),
            "description": str(fn.get("description") or ""),
            "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
        }
        declarations.append(declaration)
    if declarations:
        request_body["tools"] = [{"functionDeclarations": declarations}]
        choice = payload.get("tool_choice")
        mode = "AUTO"
        allowed: List[str] = []
        if isinstance(choice, str):
            mode = {"none": "NONE", "required": "ANY", "auto": "AUTO"}.get(choice.lower(), "AUTO")
        elif isinstance(choice, dict):
            fn = choice.get("function") or {}
            name = str(fn.get("name") or "").strip()
            if name:
                mode = "ANY"
                allowed = [original_to_safe.get(name, name)]
        function_config: Dict[str, Any] = {"mode": mode}
        if allowed:
            function_config["allowedFunctionNames"] = allowed
        request_body["toolConfig"] = {"functionCallingConfig": function_config}

    session_source = json.dumps(payload.get("messages") or [], sort_keys=True, ensure_ascii=False).encode()
    session_id = str(int.from_bytes(hashlib.sha256(session_source).digest()[:8], "big"))
    envelope: Dict[str, Any] = {
        "model": model,
        "project": project_id,
        "userAgent": "antigravity",
        "requestType": "agent",
        "requestId": "agent-" + str(uuid.uuid4()),
        "request": request_body,
    }
    request_body["sessionId"] = session_id
    return model, envelope, safe_to_original


def _created_at(response: Dict[str, Any]) -> int:
    value = response.get("createTime")
    if isinstance(value, str):
        try:
            return int(dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
        except ValueError:
            pass
    return int(time.time())


def _usage(value: Any) -> Dict[str, Any]:
    data = value if isinstance(value, dict) else {}
    prompt = int(data.get("promptTokenCount") or 0)
    completion = int(data.get("candidatesTokenCount") or 0)
    total = int(data.get("totalTokenCount") or prompt + completion)
    result: Dict[str, Any] = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
    }
    cached = int(data.get("cachedContentTokenCount") or 0)
    thoughts = int(data.get("thoughtsTokenCount") or 0)
    if cached:
        result["prompt_tokens_details"] = {"cached_tokens": cached}
    if thoughts:
        result["completion_tokens_details"] = {"reasoning_tokens": thoughts}
    return result


def _finish_reason(native: str, has_tools: bool) -> str:
    if has_tools:
        return "tool_calls"
    return "length" if native.upper() == "MAX_TOKENS" else "stop"


def antigravity_to_openai(
    upstream: Dict[str, Any], requested_model: str, name_map: Dict[str, str]
) -> Dict[str, Any]:
    response = upstream.get("response") if isinstance(upstream.get("response"), dict) else upstream
    candidates = response.get("candidates") or []
    candidate = candidates[0] if candidates and isinstance(candidates[0], dict) else {}
    content = candidate.get("content") if isinstance(candidate.get("content"), dict) else {}
    text: List[str] = []
    reasoning: List[str] = []
    tool_calls: List[Dict[str, Any]] = []
    images: List[Dict[str, Any]] = []
    for part in content.get("parts") or []:
        if not isinstance(part, dict):
            continue
        if "text" in part:
            target = reasoning if part.get("thought") else text
            target.append(str(part.get("text") or ""))
        call = part.get("functionCall")
        if isinstance(call, dict):
            safe_name = str(call.get("name") or "")
            tool_calls.append(
                {
                    "id": str(call.get("id") or f"call_{uuid.uuid4().hex}"),
                    "type": "function",
                    "function": {
                        "name": name_map.get(safe_name, safe_name),
                        "arguments": json.dumps(call.get("args") or {}, ensure_ascii=False, separators=(",", ":")),
                    },
                }
            )
        inline = part.get("inlineData") or part.get("inline_data")
        if isinstance(inline, dict) and inline.get("data"):
            mime = inline.get("mimeType") or inline.get("mime_type") or "image/png"
            images.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{inline['data']}"},
                }
            )
    message: Dict[str, Any] = {"role": "assistant", "content": "".join(text)}
    if reasoning:
        message["reasoning_content"] = "".join(reasoning)
    if tool_calls:
        message["tool_calls"] = tool_calls
    if images:
        message["images"] = images
    native_finish = str(candidate.get("finishReason") or "STOP")
    model = str(response.get("modelVersion") or requested_model)
    return {
        "id": str(response.get("responseId") or f"chatcmpl-{uuid.uuid4().hex}"),
        "object": "chat.completion",
        "created": _created_at(response),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": _finish_reason(native_finish, bool(tool_calls)),
                "native_finish_reason": native_finish.lower(),
            }
        ],
        "usage": _usage(response.get("usageMetadata")),
        "system_fingerprint": FINGERPRINT,
    }


class StreamState:
    def __init__(self, requested_model: str, name_map: Dict[str, str]) -> None:
        self.requested_model = requested_model
        self.name_map = name_map
        self.id = "chatcmpl-" + uuid.uuid4().hex
        self.created = int(time.time())
        self.model = requested_model
        self.sent_role = False
        self.tool_index = 0
        self.saw_tools = False

    def chunks(self, upstream: Dict[str, Any]) -> List[Dict[str, Any]]:
        response = upstream.get("response") if isinstance(upstream.get("response"), dict) else upstream
        if response.get("responseId"):
            self.id = str(response["responseId"])
        if response.get("modelVersion"):
            self.model = str(response["modelVersion"])
        self.created = _created_at(response)
        candidates = response.get("candidates") or []
        candidate = candidates[0] if candidates and isinstance(candidates[0], dict) else {}
        content = candidate.get("content") if isinstance(candidate.get("content"), dict) else {}
        output: List[Dict[str, Any]] = []
        for part in content.get("parts") or []:
            if not isinstance(part, dict):
                continue
            delta: Dict[str, Any] = {}
            if not self.sent_role:
                delta["role"] = "assistant"
                self.sent_role = True
            if "text" in part:
                key = "reasoning_content" if part.get("thought") else "content"
                delta[key] = str(part.get("text") or "")
            call = part.get("functionCall")
            if isinstance(call, dict):
                safe_name = str(call.get("name") or "")
                delta["tool_calls"] = [
                    {
                        "index": self.tool_index,
                        "id": str(call.get("id") or f"call_{uuid.uuid4().hex}"),
                        "type": "function",
                        "function": {
                            "name": self.name_map.get(safe_name, safe_name),
                            "arguments": json.dumps(call.get("args") or {}, ensure_ascii=False, separators=(",", ":")),
                        },
                    }
                ]
                self.tool_index += 1
                self.saw_tools = True
            inline = part.get("inlineData") or part.get("inline_data")
            if isinstance(inline, dict) and inline.get("data"):
                mime = inline.get("mimeType") or inline.get("mime_type") or "image/png"
                delta["images"] = [
                    {
                        "index": 0,
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{inline['data']}"},
                    }
                ]
            if delta:
                output.append(self._chunk(delta, None, None))
        native = str(candidate.get("finishReason") or "")
        usage = response.get("usageMetadata")
        if native and isinstance(usage, dict):
            output.append(self._chunk({}, _finish_reason(native, self.saw_tools), native.lower(), _usage(usage)))
        return output

    def _chunk(
        self,
        delta: Dict[str, Any],
        finish: Optional[str],
        native: Optional[str],
        usage: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "id": self.id,
            "object": "chat.completion.chunk",
            "created": self.created,
            "model": self.model,
            "choices": [
                {
                    "index": 0,
                    "delta": delta,
                    "finish_reason": finish,
                    "native_finish_reason": native,
                }
            ],
            "system_fingerprint": FINGERPRINT,
        }
        if usage is not None:
            result["usage"] = usage
        return result


def _iter_sse(response) -> Iterable[Dict[str, Any]]:
    data_lines: List[str] = []
    for raw in response:
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if not line:
            if data_lines:
                value = "\n".join(data_lines)
                data_lines.clear()
                if value != "[DONE]":
                    try:
                        parsed = json.loads(value)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(parsed, dict):
                        yield parsed
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
        elif line.startswith("{"):
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                yield parsed
    if data_lines:
        try:
            parsed = json.loads("\n".join(data_lines))
        except json.JSONDecodeError:
            return
        if isinstance(parsed, dict):
            yield parsed


class AntigravityGateway:
    def __init__(self, store: Optional[TokenStore] = None) -> None:
        self.store = store or TokenStore()

    def _open(
        self, payload: Dict[str, Any], stream: bool
    ) -> Tuple[Any, str, Dict[str, str]]:
        access_token, record = self.store.refresh()
        project_id = str(record.get("project_id") or "").strip()
        if not project_id:
            project_id = discover_project(access_token)
            record["project_id"] = project_id
            self.store.save(record)
        model, envelope, name_map = openai_to_antigravity(payload, project_id)
        path = "/v1internal:streamGenerateContent?alt=sse" if stream else "/v1internal:generateContent"
        encoded = json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        last_error: Optional[GatewayError] = None
        refreshed = False
        for base in Config.base_urls:
            for _ in range(2):
                request = urllib.request.Request(base.rstrip("/") + path, data=encoded, method="POST")
                request.add_header("Content-Type", "application/json")
                request.add_header("Authorization", f"Bearer {access_token}")
                request.add_header("User-Agent", antigravity_user_agent())
                request.add_header("Accept", "text/event-stream" if stream else "application/json")
                try:
                    response = _urlopen(request, Config.timeout)
                    return response, model, name_map
                except GatewayError as exc:
                    last_error = exc
                    if exc.status == 401 and not refreshed:
                        access_token, _ = self.store.refresh(force=True)
                        refreshed = True
                        continue
                    break
        raise last_error or GatewayError("没有可用的 Antigravity 上游地址", status=503)

    def complete(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        response, model, name_map = self._open(payload, False)
        with response:
            raw = response.read(MAX_BODY_BYTES + 1)
        if len(raw) > MAX_BODY_BYTES:
            raise GatewayError("上游响应过大", status=502)
        try:
            upstream = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GatewayError("上游返回了无效 JSON", status=502) from exc
        if not isinstance(upstream, dict):
            raise GatewayError("上游响应格式错误", status=502)
        return antigravity_to_openai(upstream, model, name_map)

    def stream(self, payload: Dict[str, Any]) -> Tuple[Any, StreamState]:
        response, model, name_map = self._open(payload, True)
        return response, StreamState(model, name_map)


class Handler(BaseHTTPRequestHandler):
    server_version = "antigravity-python-gateway/1.0"

    def _authorized(self) -> bool:
        if not Config.api_key:
            return True
        authorization = self.headers.get("Authorization", "")
        supplied = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
        supplied = supplied or self.headers.get("X-API-Key", "").strip()
        return secrets.compare_digest(supplied, Config.api_key)

    def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _error(self, status: int, message: str, detail: str = "") -> None:
        error: Dict[str, Any] = {"message": message, "type": "antigravity_gateway_error"}
        if detail:
            error["detail"] = detail
        self._send_json(status, {"error": error})

    def do_GET(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/health"):
            store = TokenStore()
            self._send_json(
                200,
                {
                    "status": "ok",
                    "service": FINGERPRINT,
                    "authenticated": store.exists(),
                    "endpoint": "/v1/chat/completions",
                },
            )
            return
        if path == "/v1/models":
            if not self._authorized():
                self._error(401, "Unauthorized")
                return
            now = int(time.time())
            self._send_json(
                200,
                {
                    "object": "list",
                    "data": [
                        {"id": model, "object": "model", "created": now, "owned_by": "antigravity"}
                        for model in Config.models
                    ],
                },
            )
            return
        self._error(404, "Not found")

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path != "/v1/chat/completions":
            self._error(404, "Not found")
            return
        if not self._authorized():
            self._error(401, "Unauthorized")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._error(400, "Invalid Content-Length")
            return
        if length <= 0 or length > MAX_BODY_BYTES:
            self._error(413 if length > MAX_BODY_BYTES else 400, "Invalid request size")
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._error(400, "Invalid JSON")
            return
        if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
            self._error(400, "messages must be an array")
            return
        gateway = AntigravityGateway()
        if not payload.get("stream"):
            try:
                self._send_json(200, gateway.complete(payload))
            except GatewayError as exc:
                self._error(exc.status if 400 <= exc.status < 600 else 502, str(exc), exc.body)
            except Exception as exc:
                self._error(500, f"网关内部错误：{exc}")
            return

        try:
            upstream, state = gateway.stream(payload)
        except GatewayError as exc:
            self._error(exc.status if 400 <= exc.status < 600 else 502, str(exc), exc.body)
            return
        except Exception as exc:
            self._error(500, f"网关内部错误：{exc}")
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            with upstream:
                for event in _iter_sse(upstream):
                    for chunk in state.chunks(event):
                        data = json.dumps(chunk, ensure_ascii=False)
                        self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                        self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:
            try:
                data = json.dumps({"error": {"message": str(exc), "type": "stream_error"}}, ensure_ascii=False)
                self.wfile.write(f"data: {data}\n\ndata: [DONE]\n\n".encode("utf-8"))
                self.wfile.flush()
            except Exception:
                pass

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {self.address_string()} {fmt % args}")


def _apply_args(args: argparse.Namespace) -> None:
    if args.host is not None:
        Config.host = args.host
    if args.port is not None:
        Config.port = args.port
    if args.timeout is not None:
        Config.timeout = args.timeout
    if args.api_key is not None:
        Config.api_key = args.api_key
    if args.auth_file is not None:
        Config.auth_file = os.path.abspath(os.path.expanduser(args.auth_file))
    if args.models:
        Config.models = [item.strip() for item in args.models.split(",") if item.strip()]
    if args.callback_port is not None:
        Config.callback_port = args.callback_port


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Antigravity OAuth OpenAI-compatible gateway")
    parser.add_argument("command", nargs="?", choices=("serve", "login", "auth-status"), default="serve")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--timeout", type=int)
    parser.add_argument("--api-key")
    parser.add_argument("--auth-file")
    parser.add_argument("--models", help="comma-separated model IDs")
    parser.add_argument("--callback-port", type=int)
    browser_group = parser.add_mutually_exclusive_group()
    browser_group.add_argument("--open-browser", action="store_true")
    browser_group.add_argument("--no-browser", action="store_true")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    _apply_args(args)
    if args.command == "login":
        oauth_login(Config.auth_file, Config.callback_port, args.open_browser and not args.no_browser)
        return 0
    if args.command == "auth-status":
        store = TokenStore()
        if not store.exists():
            print("未登录")
            return 1
        record = store.load()
        print(f"账号：{record.get('email') or 'unknown'}")
        print(f"项目：{record.get('project_id') or 'unknown'}")
        print(f"到期：{record.get('expired') or 'unknown'}")
        print(f"凭证：{store.path}")
        return 0
    if not TokenStore().exists():
        print("ERROR: 尚未登录 Antigravity，请先运行：python server.py login", flush=True)
        return 1
    os.makedirs(RUNTIME_DIR, exist_ok=True)
    httpd = ThreadingHTTPServer((Config.host, Config.port), Handler)
    print(
        f"{FINGERPRINT} 监听 http://{Config.host}:{Config.port}/v1/chat/completions",
        flush=True,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
