"""Codex CLI -> OpenAI-compatible local HTTP gateway.

The gateway deliberately exposes only a small, dependency-free API surface:
``GET /``, ``GET /health``, ``GET /v1/models`` and
``POST /v1/chat/completions``.  A stable HeySure session header is mapped to a
persisted Codex thread and subsequent requests use ``codex exec resume``.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shlex
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from cli_gateway.shared import content_text as _content_text
from cli_gateway.shared import fingerprint as _history_hash
from cli_gateway.shared import load_json_object as _load_json
from cli_gateway.shared import save_json_object as _save_json


CLI_ROOT = Path(__file__).resolve().parents[2]
BASE_DIR = str(CLI_ROOT / "codex_cli_api")
RUNTIME_DIR = os.path.join(BASE_DIR, "runtime")
FINGERPRINT = "codex-cli-gateway"
MAX_BODY_BYTES = 16 * 1024 * 1024


class Config:
    command = os.environ.get("CODEX_CLI_COMMAND", "codex")
    host = os.environ.get("CODEX_CLI_HOST", "127.0.0.1")
    port = int(os.environ.get("CODEX_CLI_PORT", "8120") or 8120)
    timeout = int(os.environ.get("CODEX_CLI_TIMEOUT", "900") or 900)
    api_key = os.environ.get("CODEX_CLI_API_KEY", "")
    # Optional manual override.  Empty means: discover from the installed CLI.
    models = [x.strip() for x in os.environ.get("CODEX_CLI_MODELS", "").split(",") if x.strip()]
    sandbox = os.environ.get("CODEX_CLI_SANDBOX", "read-only")
    sessions_dir = os.path.abspath(
        os.path.expanduser(os.environ.get("CODEX_CLI_SESSIONS_DIR", os.path.join(RUNTIME_DIR, "sessions")))
    )


SYSTEM_WRAPPER = """你正在作为一个通用 AI 成员响应 HeySure 平台，而不是在帮助用户修改当前网关机器上的代码。
请严格遵循下方 [系统设定] 和 [对话记录]，直接延续对话，不要输出 User/Assistant 等角色前缀。

HeySure 平台工具不是你本机可直接调用的 Codex 工具。若需要调用平台工具，必须在正文中输出且只输出可执行的 XML 文本块：
<mcp-call>{\"tool\":\"工具名\",\"arguments\":{...}}</mcp-call>
不要声称已经调用了未通过上述文本块调用的工具，也不要用本机 shell 模拟平台工具。若无需工具，正常回答即可。
"""

_ROLE_NAMES = {"system": "System", "user": "User", "assistant": "Assistant", "tool": "Tool Result"}
_LOCKS: Dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()
_MODEL_CACHE: Dict[str, Any] = {"expires_at": 0.0, "models": []}
_MODEL_CACHE_LOCK = threading.Lock()
MODEL_CACHE_SECONDS = 300
DEFAULT_MODEL_ALIAS = "codex-default"


class GatewayError(RuntimeError):
    def __init__(self, message: str, status: int = 500):
        super().__init__(message)
        self.status = status


def _command_prefix() -> List[str]:
    if os.name == "nt":
        return [Config.command]
    return shlex.split(Config.command)


def discover_models(refresh: bool = False) -> List[str]:
    """Return the visible model slugs reported by the installed Codex CLI."""
    if Config.models:
        return list(Config.models)
    now = time.monotonic()
    with _MODEL_CACHE_LOCK:
        cached = _MODEL_CACHE.get("models")
        if not refresh and cached and float(_MODEL_CACHE.get("expires_at") or 0) > now:
            return list(cached)
        try:
            completed = subprocess.run(
                _command_prefix() + ["debug", "models"],
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=min(Config.timeout, 30),
                check=False,
            )
            payload = json.loads(completed.stdout) if completed.returncode == 0 else {}
            entries = payload.get("models") if isinstance(payload, dict) else []
            visible: List[Tuple[int, str]] = []
            for entry in entries if isinstance(entries, list) else []:
                if not isinstance(entry, dict) or entry.get("visibility") == "hide":
                    continue
                slug = str(entry.get("slug") or "").strip()
                if slug:
                    visible.append((int(entry.get("priority") or 999999), slug))
            visible.sort(key=lambda item: item[0])
            models = list(dict.fromkeys(slug for _, slug in visible))
        except (OSError, ValueError, subprocess.TimeoutExpired):
            models = []
        _MODEL_CACHE["models"] = models
        _MODEL_CACHE["expires_at"] = now + MODEL_CACHE_SECONDS
        return list(models)


def _canonical_messages(messages: Any) -> List[Dict[str, Any]]:
    if not isinstance(messages, list) or not messages:
        raise GatewayError("messages 必须是非空数组", 400)
    result: List[Dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict) or not message.get("role"):
            raise GatewayError("每条 message 都必须包含 role", 400)
        result.append(copy.deepcopy(message))
    return result


def _render_messages(messages: List[Dict[str, Any]], include_wrapper: bool) -> str:
    chunks = [SYSTEM_WRAPPER.strip()] if include_wrapper else []
    if include_wrapper:
        chunks.append("[对话记录]")
    for message in messages:
        role = _ROLE_NAMES.get(str(message.get("role")), str(message.get("role", "Message")).title())
        text = _content_text(message.get("content"))
        if message.get("name"):
            role += f" ({message['name']})"
        if message.get("tool_calls"):
            text += "\n" + json.dumps(message["tool_calls"], ensure_ascii=False)
        if message.get("tool_call_id"):
            text = f"tool_call_id={message['tool_call_id']}\n{text}"
        chunks.append(f"{role}:\n{text}")
    return "\n\n".join(chunks)


def _render_tools(tools: Any) -> str:
    if not isinstance(tools, list) or not tools:
        return ""
    descriptions = []
    for tool in tools:
        function = tool.get("function", {}) if isinstance(tool, dict) else {}
        name = str(function.get("name") or "").strip()
        if not name:
            continue
        descriptions.append({
            "name": name,
            "description": function.get("description", ""),
            "parameters": function.get("parameters", {"type": "object", "properties": {}}),
        })
    if not descriptions:
        return ""
    return "\n\n[本轮可用的 HeySure 平台工具]\n" + json.dumps(descriptions, ensure_ascii=False, indent=2)


def _session_identity(payload: Dict[str, Any]) -> str:
    explicit = str(payload.get("_heysure_session_id") or payload.get("user") or "").strip()
    if explicit:
        return explicit
    # Calls without a session identity are intentionally stateless across changed histories,
    # but identical retries still map to the same cache entry.
    return "history:" + _history_hash(_canonical_messages(payload.get("messages")))


def _session_lock(key: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.Lock())


def _is_prefix(old: List[Dict[str, Any]], new: List[Dict[str, Any]]) -> bool:
    return len(old) <= len(new) and old == new[: len(old)]


def _parse_codex_jsonl(stdout: str) -> Tuple[str, str, Dict[str, int]]:
    thread_id = ""
    messages: List[str] = []
    usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    for raw in stdout.splitlines():
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "thread.started":
            thread_id = str(event.get("thread_id") or event.get("thread", {}).get("id") or "")
        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        if event.get("type") == "item.completed" and item.get("type") == "agent_message":
            text = item.get("text") or item.get("content") or ""
            if isinstance(text, list):
                text = "".join(str(x.get("text", "")) if isinstance(x, dict) else str(x) for x in text)
            if text:
                messages.append(str(text))
        event_usage = event.get("usage")
        if isinstance(event_usage, dict):
            usage["input_tokens"] = int(event_usage.get("input_tokens") or usage["input_tokens"])
            usage["output_tokens"] = int(event_usage.get("output_tokens") or usage["output_tokens"])
    usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
    return thread_id, "\n".join(messages).strip(), usage


def _reasoning_effort(value: Any) -> str:
    effort = str(value or "").strip().lower()
    return effort if effort in {"low", "medium", "high"} else ""


def _run_codex(
    prompt: str,
    model: str,
    cwd: str,
    thread_id: str = "",
    reasoning_effort: str = "",
) -> Tuple[str, str, Dict[str, int]]:
    # Parent ``exec`` options must precede the ``resume`` subcommand.  In
    # particular, ``codex exec resume --help`` does not expose --sandbox.
    argv = _command_prefix() + ["exec", "--json", "--skip-git-repo-check", "--sandbox", Config.sandbox]
    if model:
        argv += ["--model", model]
    effort = _reasoning_effort(reasoning_effort)
    if effort:
        argv += ["-c", f'model_reasoning_effort="{effort}"']
    if thread_id:
        argv += ["resume", thread_id, "-"]
    else:
        argv.append("-")
    os.makedirs(cwd, exist_ok=True)
    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            input=prompt,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=Config.timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise GatewayError(f"找不到 Codex CLI：{Config.command}") from exc
    except subprocess.TimeoutExpired as exc:
        raise GatewayError(f"Codex CLI 超时（{Config.timeout} 秒）", 504) from exc
    parsed_thread, answer, usage = _parse_codex_jsonl(completed.stdout or "")
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "未知错误").strip()[-4000:]
        raise GatewayError(f"Codex CLI 退出码 {completed.returncode}: {detail}", 502)
    if not answer:
        raise GatewayError("Codex CLI 未返回 agent_message", 502)
    return parsed_thread or thread_id, answer, usage


def _completion(model: str, content: str, usage: Dict[str, int], cached: bool = False) -> Dict[str, Any]:
    return {
        "id": "chatcmpl-" + uuid.uuid4().hex,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": usage,
        "system_fingerprint": FINGERPRINT,
        **({"cached": True} if cached else {}),
    }


class CodexGateway:
    def complete(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        messages = _canonical_messages(payload.get("messages"))
        requested_model = str(payload.get("model") or "").strip()
        if requested_model.lower() in ("", "auto", "default", DEFAULT_MODEL_ALIAS):
            cli_model = ""
            response_model = DEFAULT_MODEL_ALIAS
        else:
            cli_model = requested_model
            response_model = requested_model
        identity = _session_identity(payload)
        reasoning_effort = _reasoning_effort(payload.get("reasoning_effort"))
        session_key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        with _session_lock(session_key):
            root = os.path.join(Config.sessions_dir, session_key)
            state_path = os.path.join(root, "state.json")
            state = _load_json(state_path)
            current_hash = hashlib.sha256(
                f"{_history_hash(messages)}\0{reasoning_effort}".encode("utf-8")
            ).hexdigest()
            history_mode = str(payload.get("_heysure_history_mode") or "").strip().lower()
            context_revision = str(payload.get("_heysure_context_revision") or "").strip()
            previous_revision = str(state.get("context_revision") or "").strip()
            revision_changed = bool(
                context_revision not in ("", "0")
                and context_revision != previous_revision
            )
            replace_context = history_mode == "replace" or revision_changed
            if (
                not replace_context
                and state.get("request_hash") == current_hash
                and isinstance(state.get("response"), dict)
            ):
                cached = copy.deepcopy(state["response"])
                cached["cached"] = True
                return cached

            previous = state.get("messages") if isinstance(state.get("messages"), list) else []
            can_resume = (
                not replace_context
                and bool(state.get("thread_id"))
                and _is_prefix(previous, messages)
            )
            generation = int(state.get("generation") or 0)
            if not can_resume:
                generation += 1
            workspace = os.path.join(root, f"generation-{generation:06d}")
            delta = messages[len(previous) :] if can_resume else messages
            prompt = _render_messages(delta, include_wrapper=not can_resume)
            prompt += _render_tools(payload.get("tools"))
            prompt += "\n\nAssistant:"
            thread_id, answer, usage = _run_codex(
                prompt,
                cli_model,
                workspace,
                str(state.get("thread_id") or "") if can_resume else "",
                reasoning_effort,
            )
            response = _completion(response_model, answer, usage)
            _save_json(state_path, {
                "identity": identity,
                "generation": generation,
                "thread_id": thread_id,
                # Persist Codex's generated assistant turn too.  The next OpenAI
                # request normally echoes it in messages; excluding it would send
                # that answer to the resumed thread a second time.
                "messages": messages + [{"role": "assistant", "content": answer}],
                "request_hash": current_hash,
                "response": response,
                "context_revision": context_revision or previous_revision,
                "updated_at": int(time.time()),
            })
            return response


GATEWAY = CodexGateway()


def _stream_chunks(completion: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    base = {k: completion[k] for k in ("id", "created", "model", "system_fingerprint") if k in completion}
    yield {**base, "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]}
    yield {**base, "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"content": completion["choices"][0]["message"]["content"]}, "finish_reason": None}]}
    yield {**base, "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}


class Handler(BaseHTTPRequestHandler):
    server_version = "CodexCLIGateway/1.0"

    def handle_one_request(self) -> None:
        # A TLS ClientHello starts with record type 0x16.  This gateway speaks
        # plain HTTP; letting BaseHTTPRequestHandler parse TLS bytes produces
        # pages of binary "Bad request version" noise in journald.
        try:
            first = self.rfile.peek(1)[:1]
        except (AttributeError, OSError):
            first = b""
        if first == b"\x16":
            print(
                f"[{self.log_date_time_string()}] {self.address_string()} "
                "rejected TLS handshake on plain HTTP port; use http:// or an HTTPS reverse proxy"
            )
            self.close_connection = True
            return
        super().handle_one_request()

    def log_message(self, fmt: str, *args: Any) -> None:
        message = fmt % args
        # Escape control/non-ASCII bytes from malformed requests so systemd
        # never records opaque "blob data" entries.
        safe = message.encode("unicode_escape", "backslashreplace").decode("ascii")
        print(f"[{self.log_date_time_string()}] {self.address_string()} {safe}")

    def _authorized(self) -> bool:
        if not Config.api_key:
            return True
        return self.headers.get("Authorization", "") == f"Bearer {Config.api_key}"

    def _json(self, status: int, payload: Dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if not self._authorized():
            self._json(401, {"error": {"message": "Unauthorized", "type": "authentication_error"}})
            return
        if self.path in ("/", "/health"):
            self._json(200, {"status": "ok", "service": FINGERPRINT, "command": Config.command, "sandbox": Config.sandbox})
        elif self.path.rstrip("/") == "/v1/models":
            now = int(time.time())
            models = discover_models()
            self._json(200, {"object": "list", "data": [{"id": x, "object": "model", "created": now, "owned_by": "openai-codex-cli"} for x in models]})
        else:
            self._json(404, {"error": {"message": "Not found", "type": "not_found"}})

    def do_POST(self) -> None:
        if not self._authorized():
            self._json(401, {"error": {"message": "Unauthorized", "type": "authentication_error"}})
            return
        if self.path.rstrip("/") != "/v1/chat/completions":
            self._json(404, {"error": {"message": "Not found", "type": "not_found"}})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_BODY_BYTES:
                raise GatewayError("请求体为空或过大", 413)
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise GatewayError("请求体必须是 JSON 对象", 400)
            session_header = self.headers.get("X-HeySure-Session-ID", "").strip()
            if session_header:
                payload["_heysure_session_id"] = session_header
            history_mode = self.headers.get("X-HeySure-History-Mode", "").strip().lower()
            if history_mode:
                payload["_heysure_history_mode"] = history_mode
            context_revision = self.headers.get("X-HeySure-Context-Revision", "").strip()
            if context_revision:
                payload["_heysure_context_revision"] = context_revision
            completion = GATEWAY.complete(payload)
            if not payload.get("stream"):
                self._json(200, completion)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            for chunk in _stream_chunks(completion):
                self.wfile.write(("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode("utf-8"))
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except GatewayError as exc:
            self._json(exc.status, {"error": {"message": str(exc), "type": "gateway_error"}})
        except (ValueError, json.JSONDecodeError) as exc:
            self._json(400, {"error": {"message": f"无效 JSON: {exc}", "type": "invalid_request_error"}})
        except Exception as exc:
            self._json(500, {"error": {"message": str(exc), "type": "internal_error"}})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Codex CLI to OpenAI-compatible API gateway")
    parser.add_argument("--command", default=Config.command)
    parser.add_argument("--host", default=Config.host)
    parser.add_argument("--port", type=int, default=Config.port)
    parser.add_argument("--timeout", type=int, default=Config.timeout)
    parser.add_argument("--api-key", default=Config.api_key)
    parser.add_argument("--models", default=",".join(Config.models), help="optional comma-separated override; empty discovers from Codex CLI")
    parser.add_argument("--sandbox", choices=("read-only", "workspace-write", "danger-full-access"), default=Config.sandbox)
    parser.add_argument("--sessions-dir", default=Config.sessions_dir)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    Config.command = args.command
    Config.host = args.host
    Config.port = args.port
    Config.timeout = args.timeout
    Config.api_key = args.api_key
    Config.models = [x.strip() for x in args.models.split(",") if x.strip()]
    Config.sandbox = args.sandbox
    Config.sessions_dir = os.path.abspath(os.path.expanduser(args.sessions_dir))
    os.makedirs(Config.sessions_dir, exist_ok=True)
    models = discover_models(refresh=True)
    server = ThreadingHTTPServer((Config.host, Config.port), Handler)
    model_text = ",".join(models) if models else "<Codex CLI default; catalog unavailable>"
    print(f"Codex CLI gateway: http://{Config.host}:{Config.port}  sandbox={Config.sandbox}")
    print(f"Codex CLI models: {model_text}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
