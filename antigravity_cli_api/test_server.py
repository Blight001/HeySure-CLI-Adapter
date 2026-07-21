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
        server.TokenStore().save({
            "type": "antigravity",
            "access_token": "expired-token",
            "refresh_token": "refresh-token",
            "project_id": "student-project",
            "email": "student@example.com",
            "expired": "2000-01-01T00:00:00Z",
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


if __name__ == "__main__":
    unittest.main()
