"""Outbound HeySure external-member controller backed by the local Codex CLI."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, Optional


CLI_ROOT = Path(__file__).resolve().parents[1]
if str(CLI_ROOT) not in sys.path:
    sys.path.insert(0, str(CLI_ROOT))

from cli_gateway.backends.codex import CodexGateway, Config, GatewayError


class ControllerError(RuntimeError):
    pass


class HeySureController:
    def __init__(self, endpoint: str, token: str, timeout: int = 30):
        self.endpoint = endpoint.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.request_id = 0

    def call(self, name: str, arguments: Optional[dict] = None) -> dict:
        self.request_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self.request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        }
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[-2000:]
            raise ControllerError(f"HeySure HTTP {exc.code}: {detail}") from exc
        except (OSError, ValueError) as exc:
            raise ControllerError(f"HeySure MCP request failed: {exc}") from exc
        rpc_result = result.get("result") if isinstance(result, dict) else None
        if not isinstance(rpc_result, dict):
            raise ControllerError(f"Invalid HeySure MCP response: {result}")
        if rpc_result.get("isError"):
            content = rpc_result.get("content") or []
            message = content[0].get("text") if content and isinstance(content[0], dict) else "MCP tool failed"
            raise ControllerError(str(message))
        structured = rpc_result.get("structuredContent")
        if isinstance(structured, dict):
            return structured
        content = rpc_result.get("content") or []
        text = content[0].get("text") if content and isinstance(content[0], dict) else "{}"
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {"result": parsed}


def _messages_for_codex(context: dict, turn: dict) -> list[dict]:
    member = context.get("member") if isinstance(context.get("member"), dict) else {}
    system = "\n\n".join(
        part for part in (
            str(member.get("prompt") or "").strip(),
            "你正在通过 HeySure 外部控制桥回复当前会话。延续历史，必要时调用已配置的 HeySure MCP，并给用户一个直接、完整的答复。",
        ) if part
    )
    messages = [{"role": "system", "content": system}]
    for item in turn.get("history") or []:
        if not isinstance(item, dict) or item.get("role") not in {"user", "assistant"}:
            continue
        messages.append({"role": item["role"], "content": str(item.get("content") or "")})
    return messages


def run_once(client: HeySureController, gateway: CodexGateway, context: dict) -> bool:
    claimed = client.call(
        "heysure.claim_message",
        {"lease_seconds": 1800, "history_limit": 60},
    ).get("message")
    if not isinstance(claimed, dict):
        return False
    turn_id = str(claimed.get("turn_id") or "")
    member = context.get("member") if isinstance(context.get("member"), dict) else {}
    session_identity = f"heysure:{member.get('id') or 'member'}:{claimed.get('session_id') or uuid.uuid4().hex}"
    try:
        completion = gateway.complete({
            "model": "codex-default",
            "messages": _messages_for_codex(context, claimed),
            "user": session_identity,
            "_heysure_session_id": session_identity,
            "_heysure_native_mcp": True,
        })
        answer = str(completion["choices"][0]["message"]["content"] or "").strip()
        client.call(
            "heysure.reply_message",
            {"turn_id": turn_id, "content": answer, "model": completion.get("model") or "codex-default"},
        )
        print(f"replied turn={turn_id} session={claimed.get('session_id')}", flush=True)
    except Exception as exc:
        try:
            client.call("heysure.fail_message", {"turn_id": turn_id, "error": str(exc)[:2000]})
        except Exception as finish_exc:
            print(f"failed to close turn={turn_id}: {finish_exc}", file=sys.stderr, flush=True)
        raise
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="HeySure external MCP -> local Codex bridge")
    parser.add_argument("--endpoint", default=os.getenv("HEYSURE_CONTROLLER_URL", ""))
    parser.add_argument("--token-env", default="HEYSURE_CONTROLLER_TOKEN")
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--command", default=os.getenv("CODEX_CLI_COMMAND", Config.command))
    parser.add_argument("--workspace", default=os.getenv("CODEX_CLI_CWD", str(CLI_ROOT)))
    args = parser.parse_args()
    token = os.getenv(args.token_env, "").strip()
    if not args.endpoint or not token:
        parser.error(f"--endpoint and token environment variable {args.token_env} are required")
    Config.command = args.command
    Config.sessions_dir = os.path.abspath(os.path.join(args.workspace, ".heysure-codex-sessions"))
    client = HeySureController(args.endpoint, token)
    gateway = CodexGateway()
    context = client.call("heysure.get_context")
    stopping = False

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop)
    while not stopping:
        try:
            worked = run_once(client, gateway, context)
        except (ControllerError, GatewayError, OSError, ValueError, KeyError) as exc:
            print(f"controller error: {exc}", file=sys.stderr, flush=True)
            worked = False
        if args.once:
            break
        if not worked:
            time.sleep(max(0.5, args.poll_seconds))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
