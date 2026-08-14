"""Small ACP stdio agent used by the Grok continuity integration test."""

import json
import os
import sys
import urllib.request


TRACE_PATH = os.environ["GROK_FAKE_ACP_TRACE"]
NATIVE_SESSION_ID = "fake-native-session"


def emit(message):
    sys.stdout.buffer.write(
        (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
    )
    sys.stdout.buffer.flush()


def trace(method, params):
    record = {"method": method, "params": params}
    with open(TRACE_PATH, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        handle.flush()


def mcp_call(url):
    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 41,
        "method": "tools/call",
        "params": {"name": "lookup_status", "arguments": {"target": "seoul"}},
    }).encode("utf-8")
    request = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        response.read()


def main():
    mcp_url = ""
    for raw_line in sys.stdin.buffer:
        message = json.loads(raw_line.decode("utf-8"))
        method = str(message.get("method") or "")
        params = message.get("params") or {}
        rpc_id = message.get("id")
        trace(method, params)

        if method == "initialize":
            emit({"jsonrpc": "2.0", "id": rpc_id, "result": {}})
        elif method in ("session/new", "session/load"):
            servers = params.get("mcpServers") or []
            mcp_url = str((servers[0] if servers else {}).get("url") or "")
            if method == "session/load":
                # A real Grok load can replay old output. It must not leak into
                # the response for the new prompt.
                emit({
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {"update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "REPLAYED OLD ANSWER"},
                    }},
                })
            result = {"sessionId": NATIVE_SESSION_ID} if method == "session/new" else {}
            emit({"jsonrpc": "2.0", "id": rpc_id, "result": result})
        elif method == "session/prompt":
            prompt = "".join(
                str(item.get("text") or "")
                for item in params.get("prompt") or []
                if isinstance(item, dict)
            )
            if "[工具执行结果]" not in prompt:
                mcp_call(mcp_url)
                continue
            emit({
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {"update": {
                    "sessionUpdate": "agent_thought_chunk",
                    "content": {"type": "text", "text": "Using the existing plan."},
                }},
            })
            emit({
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {"update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "Continued after the tool result."},
                }},
            })
            emit({
                "jsonrpc": "2.0",
                "id": rpc_id,
                "result": {"stopReason": "end_turn"},
            })


if __name__ == "__main__":
    main()
