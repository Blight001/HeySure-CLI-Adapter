import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

CLI_ROOT = Path(__file__).resolve().parents[1]
if str(CLI_ROOT) not in sys.path:
    sys.path.insert(0, str(CLI_ROOT))

from cli_gateway.backends import grok as server
from cli_gateway.backends import grok_acp


FAKE_AGENT = Path(__file__).parent / "testing" / "fake_acp_agent.py"


class AcpContinuityIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.registry = grok_acp.SessionRegistry(ttl=30, max_sessions=4)
        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.http_thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.http_thread.start()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.trace_path = Path(self.temp_dir.name) / "acp-trace.jsonl"
        self.original_config = {
            "host": server.Config.host,
            "port": server.Config.port,
            "cwd": server.Config.cwd,
            "timeout": server.Config.timeout,
            "tool_grace": server.Config.tool_grace,
            "session_ttl": server.Config.session_ttl,
            "api_key": server.Config.api_key,
            "acp_enabled": server.Config.acp_enabled,
        }
        server.Config.host = "127.0.0.1"
        server.Config.port = self.httpd.server_address[1]
        server.Config.cwd = self.temp_dir.name
        server.Config.timeout = 10
        server.Config.tool_grace = 0.01
        server.Config.session_ttl = 10
        server.Config.api_key = ""
        server.Config.acp_enabled = True
        self.reasoning_effort = ""
        self.previous_persisted = dict(server._ACP_PERSISTED)
        server._ACP_PERSISTED.clear()

    def tearDown(self):
        for identity in ("continuity-test",):
            session = self.registry.get_by_identity(identity)
            if session is not None:
                self.registry.drop(session)
        server._ACP_PERSISTED.clear()
        server._ACP_PERSISTED.update(self.previous_persisted)
        for name, value in self.original_config.items():
            setattr(server.Config, name, value)
        self.httpd.shutdown()
        self.httpd.server_close()
        self.http_thread.join(timeout=5)
        self.temp_dir.cleanup()

    def chat_request(self, messages, tools, stream=False):
        payload = {
            "model": "grok-4.5",
            "messages": messages,
            "tools": tools,
            "stream": stream,
        }
        if self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort
        body = json.dumps(payload).encode("utf-8")
        return urllib.request.Request(
            f"http://127.0.0.1:{server.Config.port}/v1/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-HeySure-Session-ID": "continuity-test",
            },
        )

    def post_chat(self, messages, tools):
        request = self.chat_request(messages, tools)
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))

    def post_chat_stream(self, messages, tools):
        request = self.chat_request(messages, tools, stream=True)
        events = []
        with urllib.request.urlopen(request, timeout=15) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8").strip()
                if line.startswith("data: ") and line != "data: [DONE]":
                    events.append(json.loads(line[6:]))
        return events

    def read_trace(self):
        return [json.loads(line) for line in self.trace_path.read_text(encoding="utf-8").splitlines()]

    def test_tool_result_loads_native_session_and_continues_in_new_process(self):
        real_popen = subprocess.Popen
        launched = []
        launched_argv = []
        self.reasoning_effort = "high"

        def launch_fake(_argv, **kwargs):
            launched_argv.append(list(_argv))
            process = real_popen([sys.executable, str(FAKE_AGENT)], **kwargs)
            launched.append(process)
            return process

        tools = [{
            "type": "function",
            "function": {
                "name": "lookup_status",
                "description": "Look up a server status",
                "parameters": {
                    "type": "object",
                    "properties": {"target": {"type": "string"}},
                },
            },
        }]
        initial_messages = [{"role": "user", "content": "Check Seoul, then report."}]

        try:
            with mock.patch.object(server, "ACP_REGISTRY", self.registry), mock.patch.object(
                server, "_save_persisted_sessions_locked"
            ), mock.patch.dict(os.environ, {"GROK_FAKE_ACP_TRACE": str(self.trace_path)}), mock.patch.object(
                grok_acp.subprocess, "Popen", side_effect=launch_fake
            ):
                first = self.post_chat(initial_messages, tools)
                first_message = first["choices"][0]["message"]
                self.assertEqual(first["choices"][0]["finish_reason"], "tool_calls", first)
                call = first_message["tool_calls"][0]
                self.assertEqual(call["function"]["name"], "lookup_status")

                live_session = self.registry.get_by_identity("continuity-test")
                self.assertIsNotNone(live_session)
                self.registry.drop(live_session)

                continued_messages = initial_messages + [first_message, {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": "Seoul is healthy",
                }]
                second = self.post_chat(continued_messages, tools)
                self.assertTrue(launched_argv)
                self.assertTrue(all(
                    argv[argv.index("--reasoning-effort") + 1] == "high"
                    for argv in launched_argv
                ))
        finally:
            for process in launched:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                for pipe in (process.stdin, process.stdout, process.stderr):
                    if pipe is not None:
                        pipe.close()

        second_choice = second["choices"][0]
        self.assertEqual(second_choice["finish_reason"], "stop")
        self.assertEqual(
            second_choice["message"]["content"],
            "Continued after the tool result.",
        )
        self.assertNotIn("REPLAYED OLD ANSWER", second_choice["message"]["content"])

        trace = self.read_trace()
        new_calls = [item for item in trace if item["method"] == "session/new"]
        load_calls = [item for item in trace if item["method"] == "session/load"]
        prompts = [item for item in trace if item["method"] == "session/prompt"]
        self.assertEqual(len(new_calls), 1)
        self.assertEqual(len(load_calls), 1)
        self.assertEqual(
            load_calls[0]["params"]["sessionId"],
            "fake-native-session",
        )
        resumed_prompt = prompts[-1]["params"]["prompt"][0]["text"]
        self.assertIn("Seoul is healthy", resumed_prompt)
        self.assertIn("同一个任务正在继续", resumed_prompt)
        self.assertIn("The user wants", resumed_prompt)
        self.assertNotIn("Check Seoul, then report.", resumed_prompt)

    def test_tool_result_continues_the_same_live_prompt(self):
        real_popen = subprocess.Popen
        launched = []

        def launch_fake(_argv, **kwargs):
            process = real_popen([sys.executable, str(FAKE_AGENT)], **kwargs)
            launched.append(process)
            return process

        tools = [{
            "type": "function",
            "function": {
                "name": "lookup_status",
                "description": "Look up a server status",
                "parameters": {"type": "object", "properties": {}},
            },
        }]
        initial_messages = [{"role": "user", "content": "Check Seoul, then report."}]

        try:
            with mock.patch.object(server, "ACP_REGISTRY", self.registry), mock.patch.object(
                server, "_save_persisted_sessions_locked"
            ), mock.patch.dict(os.environ, {"GROK_FAKE_ACP_TRACE": str(self.trace_path)}), mock.patch.object(
                grok_acp.subprocess, "Popen", side_effect=launch_fake
            ):
                first = self.post_chat(initial_messages, tools)
                first_message = first["choices"][0]["message"]
                call = first_message["tool_calls"][0]
                second = self.post_chat(initial_messages + [first_message, {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": "Seoul is healthy",
                }], tools)
        finally:
            for process in launched:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                for pipe in (process.stdin, process.stdout, process.stderr):
                    if pipe is not None:
                        pipe.close()

        second_choice = second["choices"][0]
        self.assertEqual(second_choice["finish_reason"], "stop")
        self.assertEqual(
            second_choice["message"]["content"],
            "Continued after the tool result.",
        )
        trace = self.read_trace()
        self.assertEqual(len(launched), 1)
        self.assertEqual(len([item for item in trace if item["method"] == "session/new"]), 1)
        self.assertEqual(len([item for item in trace if item["method"] == "session/load"]), 0)
        self.assertEqual(len([item for item in trace if item["method"] == "session/prompt"]), 1)

    def test_streaming_tool_result_continues_the_same_live_prompt(self):
        real_popen = subprocess.Popen
        launched = []

        def launch_fake(_argv, **kwargs):
            process = real_popen([sys.executable, str(FAKE_AGENT)], **kwargs)
            launched.append(process)
            return process

        tools = [{
            "type": "function",
            "function": {
                "name": "lookup_status",
                "description": "Look up a server status",
                "parameters": {"type": "object", "properties": {}},
            },
        }]
        initial_messages = [{"role": "user", "content": "Check Seoul, then report."}]

        try:
            with mock.patch.object(server, "ACP_REGISTRY", self.registry), mock.patch.object(
                server, "_save_persisted_sessions_locked"
            ), mock.patch.dict(os.environ, {"GROK_FAKE_ACP_TRACE": str(self.trace_path)}), mock.patch.object(
                grok_acp.subprocess, "Popen", side_effect=launch_fake
            ):
                first = self.post_chat(initial_messages, tools)
                first_message = first["choices"][0]["message"]
                call = first_message["tool_calls"][0]
                events = self.post_chat_stream(initial_messages + [first_message, {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": "Seoul is healthy",
                }], tools)
        finally:
            for process in launched:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                for pipe in (process.stdin, process.stdout, process.stderr):
                    if pipe is not None:
                        pipe.close()

        deltas = [choice["delta"] for event in events for choice in event["choices"]]
        content = "".join(str(delta.get("content") or "") for delta in deltas)
        finishes = [
            choice["finish_reason"]
            for event in events
            for choice in event["choices"]
            if choice.get("finish_reason")
        ]
        self.assertEqual(content, "Continued after the tool result.")
        self.assertEqual(finishes, ["stop"])
        trace = self.read_trace()
        self.assertEqual(len(launched), 1)
        self.assertEqual(len([item for item in trace if item["method"] == "session/load"]), 0)
        self.assertEqual(len([item for item in trace if item["method"] == "session/prompt"]), 1)

    def test_live_process_exit_after_answer_falls_back_to_native_load(self):
        real_popen = subprocess.Popen
        launched = []

        def launch_fake(_argv, **kwargs):
            process = real_popen([sys.executable, str(FAKE_AGENT)], **kwargs)
            launched.append(process)
            return process

        tools = [{
            "type": "function",
            "function": {
                "name": "lookup_status",
                "description": "Look up a server status",
                "parameters": {"type": "object", "properties": {}},
            },
        }]
        initial_messages = [{"role": "user", "content": "Check Seoul, then report."}]

        try:
            with mock.patch.object(server, "ACP_REGISTRY", self.registry), mock.patch.object(
                server, "_save_persisted_sessions_locked"
            ), mock.patch.dict(os.environ, {
                "GROK_FAKE_ACP_TRACE": str(self.trace_path),
                "GROK_FAKE_ACP_EXIT_AFTER_RESULT": "1",
            }), mock.patch.object(grok_acp.subprocess, "Popen", side_effect=launch_fake):
                first = self.post_chat(initial_messages, tools)
                first_message = first["choices"][0]["message"]
                call = first_message["tool_calls"][0]
                second = self.post_chat(initial_messages + [first_message, {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": "Seoul is healthy",
                }], tools)
        finally:
            for process in launched:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                for pipe in (process.stdin, process.stdout, process.stderr):
                    if pipe is not None:
                        pipe.close()

        choice = second["choices"][0]
        self.assertEqual(choice["finish_reason"], "stop")
        self.assertEqual(choice["message"]["content"], "Continued after the tool result.")
        trace = self.read_trace()
        self.assertEqual(len(launched), 2)
        self.assertEqual(len([item for item in trace if item["method"] == "session/new"]), 1)
        self.assertEqual(len([item for item in trace if item["method"] == "session/load"]), 1)


if __name__ == "__main__":
    unittest.main()
