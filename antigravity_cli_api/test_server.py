import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import server


class FakeGoogleHandler(BaseHTTPRequestHandler):
    refreshes = 0
    requests = []

    def _json(self, value, status=200):
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith("/userinfo"):
            self._json({"email": "student@example.com"})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        if self.path == "/token":
            form = urllib.parse.parse_qs(raw.decode("utf-8"))
            self.__class__.refreshes += 1
            self.assert_form(form)
            self._json({"access_token": "refreshed-token", "expires_in": 3600})
            return

        payload = json.loads(raw.decode("utf-8"))
        self.__class__.requests.append((self.path, payload, self.headers.get("Authorization")))
        if self.path == "/v1internal:loadCodeAssist":
            self._json({"cloudaicompanionProject": "student-project"})
        elif self.path == "/v1internal:generateContent":
            self._json(self.response("测试成功"))
        elif self.path.startswith("/v1internal:streamGenerateContent"):
            first = json.dumps(self.response("测试", finish=""), ensure_ascii=False)
            second = json.dumps(self.response("成功", finish="STOP"), ensure_ascii=False)
            data = f"data: {first}\n\ndata: {second}\n\n".encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self._json({"error": "not found"}, 404)

    @staticmethod
    def assert_form(form):
        if form.get("grant_type") != ["refresh_token"]:
            raise AssertionError(form)
        if not form.get("client_id") or not form.get("client_id")[0]:
            raise AssertionError(f"missing client_id: {form}")
        if not form.get("client_secret") or not form.get("client_secret")[0]:
            raise AssertionError(f"missing client_secret: {form}")

    @staticmethod
    def response(text, finish="STOP"):
        return {
            "response": {
                "responseId": "response-1",
                "modelVersion": "gemini-test",
                "candidates": [{
                    "content": {"role": "model", "parts": [{"text": text}]},
                    "finishReason": finish,
                }],
                "usageMetadata": {
                    "promptTokenCount": 3,
                    "candidatesTokenCount": 2,
                    "totalTokenCount": 5,
                },
            }
        }

    def log_message(self, _format, *_args):
        pass


class GatewayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.saved = {
            name: getattr(server.Config, name)
            for name in (
                "auth_file", "base_urls", "token_endpoint", "userinfo_endpoint",
                "version_manifest", "user_agent", "models", "api_key",
                "client_id", "client_secret", "backend", "cli_command",
                "cli_arg_safe_bytes", "cli_sessions_dir",
            )
        }
        cls.temp = tempfile.TemporaryDirectory()
        cls.upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakeGoogleHandler)
        cls.upstream_thread = threading.Thread(target=cls.upstream.serve_forever, daemon=True)
        cls.upstream_thread.start()
        upstream_base = f"http://127.0.0.1:{cls.upstream.server_address[1]}"

        server.Config.auth_file = os.path.join(cls.temp.name, "auth.json")
        server.Config.base_urls = [upstream_base]
        server.Config.token_endpoint = upstream_base + "/token"
        server.Config.userinfo_endpoint = upstream_base + "/userinfo"
        server.Config.version_manifest = ""
        server.Config.user_agent = "antigravity/hub/test linux/amd64"
        server.Config.models = ["gemini-test"]
        server.Config.api_key = ""
        server.Config.client_id = "test-client-id"
        server.Config.client_secret = "test-client-secret"
        server.Config.backend = "direct"
        server.Config.cli_command = "agy"
        server.Config.cli_arg_safe_bytes = 96 * 1024
        server.Config.cli_sessions_dir = os.path.join(cls.temp.name, "cli-sessions")
        server.TokenStore().save({
            "type": "antigravity",
            "access_token": "expired-token",
            "refresh_token": "refresh-token",
            "project_id": "student-project",
            "email": "student@example.com",
            "expired": "2000-01-01T00:00:00Z",
            "client_id": "test-client-id",
            "client_secret": "test-client-secret",
        })

        cls.gateway = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.gateway_thread = threading.Thread(target=cls.gateway.serve_forever, daemon=True)
        cls.gateway_thread.start()
        cls.base = f"http://127.0.0.1:{cls.gateway.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.gateway.shutdown()
        cls.gateway.server_close()
        cls.gateway_thread.join(timeout=2)
        cls.upstream.shutdown()
        cls.upstream.server_close()
        cls.upstream_thread.join(timeout=2)
        for name, value in cls.saved.items():
            setattr(server.Config, name, value)
        cls.temp.cleanup()

    def request(self, payload):
        request = urllib.request.Request(
            self.base + "/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        return urllib.request.urlopen(request, timeout=10)

    def test_01_health_and_models(self):
        with urllib.request.urlopen(self.base + "/health", timeout=5) as response:
            health = json.load(response)
        self.assertEqual(health["service"], "antigravity-python-gateway")
        self.assertTrue(health["authenticated"])
        with urllib.request.urlopen(self.base + "/v1/models", timeout=5) as response:
            models = json.load(response)
        self.assertEqual(models["data"][0]["id"], "gemini-test")

    def test_02_translation_preserves_tools(self):
        model, envelope, reverse = server.openai_to_antigravity({
            "model": "auto",
            "messages": [
                {"role": "system", "content": "只说中文"},
                {"role": "user", "content": "查询天气"},
            ],
            "tools": [{"type": "function", "function": {
                "name": "weather.lookup", "description": "查天气",
                "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
            }}],
        }, "student-project")
        request = envelope["request"]
        safe_name = request["tools"][0]["functionDeclarations"][0]["name"]
        self.assertEqual(model, "gemini-test")
        self.assertEqual(reverse[safe_name], "weather.lookup")
        self.assertEqual(request["systemInstruction"]["parts"][0]["text"], "只说中文")

    def test_03_blocking_completion_refreshes_token(self):
        with self.request({
            "model": "auto",
            "messages": [{"role": "user", "content": "say ok"}],
        }) as response:
            payload = json.load(response)
        self.assertEqual(payload["choices"][0]["message"]["content"], "测试成功")
        self.assertEqual(payload["usage"]["total_tokens"], 5)
        self.assertEqual(payload["system_fingerprint"], "antigravity-python-gateway")
        self.assertEqual(FakeGoogleHandler.refreshes, 1)
        self.assertEqual(FakeGoogleHandler.requests[-1][2], "Bearer refreshed-token")

    def test_04_streaming_completion_emits_deltas(self):
        with self.request({
            "model": "gemini-test", "stream": True,
            "messages": [{"role": "user", "content": "say ok"}],
        }) as response:
            text = response.read().decode("utf-8")
        self.assertIn('"content": "测试"', text)
        self.assertIn('"content": "成功"', text)
        self.assertTrue(text.rstrip().endswith("data: [DONE]"))

    def test_05_callback_parser(self):
        parsed = server._parse_callback("http://localhost:51121/oauth-callback?code=abc&state=xyz")
        self.assertEqual(parsed, {"code": "abc", "state": "xyz", "error": ""})

    def test_06_gateway_api_key(self):
        server.Config.api_key = "local-secret"
        try:
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(self.base + "/v1/models", timeout=5)
            self.assertEqual(caught.exception.code, 401)
            caught.exception.close()
            request = urllib.request.Request(
                self.base + "/v1/models",
                headers={"Authorization": "Bearer local-secret"},
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                self.assertEqual(response.status, 200)
        finally:
            server.Config.api_key = ""

    def test_07_refresh_uses_auth_file_client_when_env_empty(self):
        saved_id, saved_secret = server.Config.client_id, server.Config.client_secret
        try:
            server.Config.client_id = ""
            server.Config.client_secret = ""
            store = server.TokenStore()
            store.save({
                "type": "antigravity",
                "access_token": "stale",
                "refresh_token": "refresh-token",
                "project_id": "student-project",
                "email": "student@example.com",
                "expired": "2000-01-01T00:00:00Z",
                "client_id": "file-client-id",
                "client_secret": "file-client-secret",
            })
            token, record = store.refresh(force=True)
            self.assertEqual(token, "refreshed-token")
            self.assertEqual(record.get("client_id"), "file-client-id")
            self.assertEqual(server.Config.client_id, "file-client-id")
        finally:
            server.Config.client_id = saved_id
            server.Config.client_secret = saved_secret
            server.TokenStore().save({
                "type": "antigravity",
                "access_token": "refreshed-token",
                "refresh_token": "refresh-token",
                "project_id": "student-project",
                "email": "student@example.com",
                "expired": "2099-01-01T00:00:00Z",
                "client_id": "test-client-id",
                "client_secret": "test-client-secret",
            })

    def test_08_refresh_fails_clearly_without_client_id(self):
        saved_id, saved_secret = server.Config.client_id, server.Config.client_secret
        try:
            server.Config.client_id = ""
            server.Config.client_secret = ""
            store = server.TokenStore()
            store.save({
                "type": "antigravity",
                "access_token": "stale",
                "refresh_token": "refresh-token",
                "project_id": "student-project",
                "expired": "2000-01-01T00:00:00Z",
            })
            with self.assertRaises(server.GatewayError) as caught:
                store.refresh(force=True)
            self.assertIn("OAuth Client", str(caught.exception))
        finally:
            server.Config.client_id = saved_id
            server.Config.client_secret = saved_secret

    def test_09_cli_backend_uses_official_command_without_credentials(self):
        payload = {
            "model": "auto",
            "messages": [
                {"role": "system", "content": "只说中文"},
                {"role": "user", "content": "查询天气"},
            ],
            "tools": [{"type": "function", "function": {
                "name": "weather.lookup",
                "description": "查天气",
                "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
            }}],
        }
        completed = mock.Mock(returncode=0, stdout="\x1b[32m测试成功\x1b[0m\n", stderr="")
        saved_models = server.Config.models
        server.Config.models = ["Gemini Test"]
        try:
            with mock.patch.object(server.subprocess, "run", return_value=completed) as run:
                result = server.AntigravityCLIGateway().complete(payload)
            argv = run.call_args.args[0]
            self.assertEqual(argv[:3], ["agy", "--model", "Gemini Test"])
            prompt = argv[-1]
            self.assertIn("weather.lookup", prompt)
            self.assertIn("<mcp-call>", prompt)
            self.assertNotIn("client_secret", " ".join(argv).lower())
            self.assertEqual(result["choices"][0]["message"]["content"], "测试成功")
        finally:
            server.Config.models = saved_models

    def test_10_stateful_cli_sends_only_incremental_messages(self):
        first = mock.Mock(returncode=0, stdout="第一次回复\n", stderr="")
        second = mock.Mock(returncode=0, stdout="第二次回复\n", stderr="")
        gateway = server.AntigravityCLIGateway()
        initial = {
            "user": "heysure-session-incremental",
            "model": "Gemini Test",
            "messages": [
                {"role": "system", "content": "只说中文"},
                {"role": "user", "content": "第一问"},
            ],
        }
        follow_up = {
            **initial,
            "messages": initial["messages"] + [
                {"role": "assistant", "content": "第一次回复"},
                {"role": "user", "content": "第二问"},
            ],
        }
        with mock.patch.object(server.subprocess, "run", side_effect=[first, second]) as run:
            gateway.complete(initial)
            result = gateway.complete(follow_up)
        first_argv = run.call_args_list[0].args[0]
        second_argv = run.call_args_list[1].args[0]
        self.assertNotIn("--continue", first_argv)
        self.assertIn("--continue", second_argv)
        self.assertIn("第二问", second_argv[-1])
        self.assertNotIn("只说中文", second_argv[-1])
        self.assertEqual(
            run.call_args_list[0].kwargs["cwd"],
            run.call_args_list[1].kwargs["cwd"],
        )
        self.assertEqual(result["choices"][0]["message"]["content"], "第二次回复")

    def test_11_stateful_cli_retries_return_cached_response(self):
        completed = mock.Mock(returncode=0, stdout="不会重复扣额度\n", stderr="")
        payload = {
            "user": "heysure-session-retry",
            "model": "Gemini Test",
            "messages": [{"role": "user", "content": "幂等测试"}],
        }
        gateway = server.AntigravityCLIGateway()
        with mock.patch.object(server.subprocess, "run", return_value=completed) as run:
            gateway.complete(payload)
            retried = gateway.complete(payload)
        self.assertEqual(run.call_count, 1)
        self.assertTrue(retried.get("cached"))
        self.assertEqual(retried["choices"][0]["message"]["content"], "不会重复扣额度")

    def test_12_long_prompt_is_attached_from_local_file(self):
        saved_limit = server.Config.cli_arg_safe_bytes
        server.Config.cli_arg_safe_bytes = 8192
        captured = {}

        def fake_run(argv, **kwargs):
            short_prompt = argv[-1]
            self.assertIn("view_file", short_prompt)
            self.assertIn("AbsolutePath", short_prompt)
            self.assertIn("禁止使用 command", short_prompt)
            prompt_name = next(
                name for name in os.listdir(kwargs["cwd"])
                if name.startswith("heysure-prompt-") and name.endswith(".md")
            )
            prompt_path = os.path.join(kwargs["cwd"], prompt_name)
            self.assertIn(prompt_path, short_prompt)
            self.assertTrue(os.path.isfile(prompt_path))
            with open(prompt_path, "r", encoding="utf-8") as handle:
                captured["content"] = handle.read()
            captured["path"] = prompt_path
            captured["argv_prompt"] = short_prompt
            return mock.Mock(returncode=0, stdout="长上下文成功\n", stderr="")

        payload = {
            "user": "heysure-session-long-prompt",
            "model": "Gemini Test",
            "messages": [{"role": "user", "content": "长内容" * 5000}],
        }
        try:
            with mock.patch.object(server.subprocess, "run", side_effect=fake_run):
                result = server.AntigravityCLIGateway().complete(payload)
        finally:
            server.Config.cli_arg_safe_bytes = saved_limit
        self.assertIn("长内容" * 100, captured["content"])
        self.assertLess(len(captured["argv_prompt"].encode("utf-8")), 8192)
        self.assertFalse(os.path.exists(captured["path"]))
        self.assertEqual(result["choices"][0]["message"]["content"], "长上下文成功")

    def test_13_http_session_header_reaches_cli_gateway(self):
        captured = {}

        def fake_complete(_gateway, payload):
            captured.update(payload)
            return server._openai_completion("Gemini Test", "header ok", "test")

        saved_backend = server.Config.backend
        server.Config.backend = "cli"
        request = urllib.request.Request(
            self.base + "/v1/chat/completions",
            data=json.dumps({
                "model": "Gemini Test",
                "messages": [{"role": "user", "content": "test"}],
            }).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-HeySure-Session-ID": "anonymous-session-id",
            },
        )
        try:
            with mock.patch.object(server.AntigravityCLIGateway, "complete", fake_complete):
                with urllib.request.urlopen(request, timeout=5) as response:
                    self.assertEqual(json.load(response)["choices"][0]["message"]["content"], "header ok")
        finally:
            server.Config.backend = saved_backend
        self.assertEqual(captured.get("_heysure_session_id"), "anonymous-session-id")

    def test_14_rewritten_history_starts_a_new_cli_generation(self):
        completed = mock.Mock(returncode=0, stdout="回复\n", stderr="")
        gateway = server.AntigravityCLIGateway()
        initial = {
            "user": "heysure-session-rewritten-history",
            "model": "Gemini Test",
            "messages": [{"role": "user", "content": "原始问题"}],
        }
        rewritten = {
            **initial,
            "messages": [{"role": "user", "content": "压缩后的上下文"}],
        }
        with mock.patch.object(server.subprocess, "run", return_value=completed) as run:
            gateway.complete(initial)
            gateway.complete(rewritten)
        first_call, second_call = run.call_args_list
        self.assertNotIn("--continue", first_call.args[0])
        self.assertNotIn("--continue", second_call.args[0])
        self.assertNotEqual(first_call.kwargs["cwd"], second_call.kwargs["cwd"])
        self.assertTrue(second_call.kwargs["cwd"].endswith("generation-000001"))

    def test_15_empty_stdout_is_recovered_from_current_transcript(self):
        conversation_id = "12345678-1234-1234-1234-123456789abc"
        data_root = os.path.join(self.temp.name, "agy-data")
        transcript_dir = os.path.join(
            data_root,
            "brain",
            conversation_id,
            ".system_generated",
            "logs",
        )
        os.makedirs(transcript_dir, exist_ok=True)
        transcript_path = os.path.join(transcript_dir, "transcript.jsonl")
        with open(transcript_path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"type": "USER_INPUT", "content": "旧问题"}) + "\n")
            handle.write(json.dumps({
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "content": "旧回复",
            }, ensure_ascii=False) + "\n")
            handle.write(json.dumps({"type": "USER_INPUT", "content": "新问题"}) + "\n")
            handle.write(json.dumps({
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "content": "从 transcript 恢复的回复",
            }, ensure_ascii=False) + "\n")

        def fake_run(argv, **_kwargs):
            log_path = argv[argv.index("--log-file") + 1]
            with open(log_path, "w", encoding="utf-8") as handle:
                handle.write(f"Print mode: conversation={conversation_id}\n")
            return mock.Mock(returncode=0, stdout="", stderr="")

        payload = {
            "user": "heysure-session-transcript-recovery",
            "model": "gemini-3.5-flash-low",
            "messages": [{"role": "user", "content": "恢复测试"}],
        }
        with mock.patch.dict(os.environ, {"ANTIGRAVITY_CLI_DATA_DIR": data_root}):
            with mock.patch.object(server.subprocess, "run", side_effect=fake_run):
                result = server.AntigravityCLIGateway().complete(payload)
        self.assertEqual(
            result["choices"][0]["message"]["content"],
            "从 transcript 恢复的回复",
        )


if __name__ == "__main__":
    unittest.main()
