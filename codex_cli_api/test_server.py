import json
import os
import tempfile
import threading
import time
import unittest
import socket
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

import server


def codex_result(thread="12345678-1234-1234-1234-123456789abc", text="测试成功"):
    stdout = "\n".join([
        json.dumps({"type": "thread.started", "thread_id": thread}),
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": text}}, ensure_ascii=False),
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 3, "output_tokens": 2}}),
    ])
    return mock.Mock(returncode=0, stdout=stdout, stderr="")


class CodexGatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.saved_dir = server.Config.sessions_dir
        server.Config.sessions_dir = self.temp.name

    def tearDown(self):
        server.Config.sessions_dir = self.saved_dir
        self.temp.cleanup()

    def test_jsonl_parser(self):
        thread, text, usage = server._parse_codex_jsonl(codex_result().stdout)
        self.assertTrue(thread.startswith("12345678"))
        self.assertEqual(text, "测试成功")
        self.assertEqual(usage["total_tokens"], 5)

    def test_model_catalog_is_discovered_from_codex_cli(self):
        catalog = {
            "models": [
                {"slug": "visible-second", "visibility": "list", "priority": 2},
                {"slug": "hidden", "visibility": "hide", "priority": 1},
                {"slug": "visible-first", "visibility": "list", "priority": 1},
            ]
        }
        completed = mock.Mock(returncode=0, stdout=json.dumps(catalog), stderr="")
        saved_models = server.Config.models
        server.Config.models = []
        server._MODEL_CACHE["expires_at"] = 0
        try:
            with mock.patch.object(server.subprocess, "run", return_value=completed) as run:
                models = server.discover_models(refresh=True)
            self.assertEqual(models, ["visible-first", "visible-second"])
            self.assertEqual(run.call_args.args[0][-2:], ["debug", "models"])
        finally:
            server.Config.models = saved_models
            server._MODEL_CACHE["expires_at"] = 0
            server._MODEL_CACHE["models"] = []

    def test_default_model_alias_does_not_force_cli_model(self):
        payload = {
            "user": "session-default-model",
            "model": "codex-default",
            "messages": [{"role": "user", "content": "hello"}],
        }
        with mock.patch.object(server.subprocess, "run", return_value=codex_result()) as run:
            result = server.CodexGateway().complete(payload)
        self.assertNotIn("--model", run.call_args.args[0])
        self.assertEqual(result["model"], "codex-default")

    def test_new_session_then_resume_with_incremental_history(self):
        gateway = server.CodexGateway()
        first = {"user": "session-a", "model": "gpt-test", "messages": [{"role": "user", "content": "第一问"}]}
        second = {**first, "messages": first["messages"] + [
            {"role": "assistant", "content": "测试成功"},
            {"role": "user", "content": "第二问"},
        ]}
        with mock.patch.object(server.subprocess, "run", side_effect=[codex_result(), codex_result(text="第二答")]) as run:
            gateway.complete(first)
            result = gateway.complete(second)
        first_argv = run.call_args_list[0].args[0]
        second_argv = run.call_args_list[1].args[0]
        self.assertNotIn("resume", first_argv)
        self.assertIn("resume", second_argv)
        self.assertLess(second_argv.index("--sandbox"), second_argv.index("resume"))
        self.assertIn("第一问", run.call_args_list[0].kwargs["input"])
        self.assertNotIn("第一问", run.call_args_list[1].kwargs["input"])
        self.assertNotIn("测试成功", run.call_args_list[1].kwargs["input"])
        self.assertIn("第二问", run.call_args_list[1].kwargs["input"])
        self.assertEqual(result["choices"][0]["message"]["content"], "第二答")

    def test_identical_retry_is_cached(self):
        payload = {"user": "session-cache", "messages": [{"role": "user", "content": "hello"}]}
        with mock.patch.object(server.subprocess, "run", return_value=codex_result()) as run:
            server.CodexGateway().complete(payload)
            result = server.CodexGateway().complete(payload)
        self.assertEqual(run.call_count, 1)
        self.assertTrue(result["cached"])

    def test_rewritten_history_starts_new_thread(self):
        gateway = server.CodexGateway()
        first = {"user": "session-rewrite", "messages": [{"role": "user", "content": "原问题"}]}
        rewritten = {"user": "session-rewrite", "messages": [{"role": "user", "content": "压缩上下文"}]}
        with mock.patch.object(server.subprocess, "run", return_value=codex_result()) as run:
            gateway.complete(first)
            gateway.complete(rewritten)
        self.assertNotIn("resume", run.call_args_list[1].args[0])
        self.assertNotEqual(run.call_args_list[0].kwargs["cwd"], run.call_args_list[1].kwargs["cwd"])

    def test_context_revision_forces_new_thread_with_full_compressed_history(self):
        gateway = server.CodexGateway()
        first = {
            "user": "session-compress",
            "_heysure_history_mode": "full",
            "_heysure_context_revision": "0",
            "messages": [{"role": "user", "content": "原问题"}],
        }
        compressed = {
            **first,
            "_heysure_context_revision": "revision-2",
            "messages": [
                {"role": "system", "content": "历史摘要：记住代号青鸟"},
                {"role": "user", "content": "代号是什么？"},
            ],
        }
        with mock.patch.object(server.subprocess, "run", return_value=codex_result()) as run:
            gateway.complete(first)
            gateway.complete(compressed)
        self.assertNotIn("resume", run.call_args_list[1].args[0])
        self.assertIn("历史摘要：记住代号青鸟", run.call_args_list[1].kwargs["input"])
        self.assertNotEqual(run.call_args_list[0].kwargs["cwd"], run.call_args_list[1].kwargs["cwd"])

    def test_tools_are_prompted_as_text_protocol(self):
        payload = {
            "user": "session-tools",
            "messages": [{"role": "user", "content": "查天气"}],
            "tools": [{"type": "function", "function": {"name": "weather.lookup", "description": "天气", "parameters": {"type": "object"}}}],
        }
        with mock.patch.object(server.subprocess, "run", return_value=codex_result()) as run:
            server.CodexGateway().complete(payload)
        prompt = run.call_args.kwargs["input"]
        self.assertIn("weather.lookup", prompt)
        self.assertIn("<mcp-call>", prompt)
        self.assertIn("--sandbox", run.call_args.args[0])

    def test_cli_failure_is_gateway_error(self):
        failed = mock.Mock(returncode=1, stdout="", stderr="not logged in")
        with mock.patch.object(server.subprocess, "run", return_value=failed):
            with self.assertRaises(server.GatewayError) as caught:
                server.CodexGateway().complete({"user": "bad", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(caught.exception.status, 502)
        self.assertIn("not logged in", str(caught.exception))

    def test_http_health_and_streaming_completion(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        completion = server._completion(
            "gpt-test",
            "HTTP 成功",
            {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )
        try:
            with urllib.request.urlopen(base + "/health", timeout=5) as response:
                self.assertEqual(json.load(response)["service"], server.FINGERPRINT)
            request = urllib.request.Request(
                base + "/v1/chat/completions",
                data=json.dumps({
                    "model": "gpt-test",
                    "stream": True,
                    "messages": [{"role": "user", "content": "test"}],
                }).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "X-HeySure-Session-ID": "http-session",
                    "X-HeySure-History-Mode": "full",
                    "X-HeySure-Context-Revision": "revision-http",
                },
            )
            with mock.patch.object(server.GATEWAY, "complete", return_value=completion) as complete:
                with urllib.request.urlopen(request, timeout=5) as response:
                    body = response.read().decode("utf-8")
            self.assertEqual(complete.call_args.args[0]["_heysure_session_id"], "http-session")
            self.assertEqual(complete.call_args.args[0]["_heysure_history_mode"], "full")
            self.assertEqual(complete.call_args.args[0]["_heysure_context_revision"], "revision-http")
            self.assertIn("HTTP 成功", body)
            self.assertTrue(body.rstrip().endswith("data: [DONE]"))
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)

    def test_tls_client_hello_is_rejected_without_binary_log_noise(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            with mock.patch("builtins.print") as printed:
                with socket.create_connection(httpd.server_address, timeout=5) as client:
                    client.sendall(b"\x16\x03\x01\x00\x10not-really-tls")
                    client.shutdown(socket.SHUT_WR)
                    client.recv(1024)
                deadline = time.time() + 2
                while not printed.called and time.time() < deadline:
                    time.sleep(0.01)
            output = " ".join(str(call.args[0]) for call in printed.call_args_list)
            self.assertIn("rejected TLS handshake", output)
            self.assertNotIn("Bad request version", output)
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
