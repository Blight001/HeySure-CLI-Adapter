import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location("heysure_cli_agent", Path(__file__).with_name("agent.py"))
agent = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(agent)


class ConfigTests(unittest.TestCase):
    def test_default_server_uses_shared_device_config(self):
        with patch.dict(agent.os.environ, {}, clear=True):
            self.assertEqual(agent._default_config()["server"], "http://49.234.181.190:58150")

    def test_local_server_requires_explicit_test_mode(self):
        with patch.dict(agent.os.environ, {"HEYSURE_LOCAL_TEST": "true"}, clear=True):
            self.assertEqual(agent._default_config()["server"], "http://127.0.0.1:3000")

    def test_explicit_server_overrides_shared_config(self):
        with patch.dict(agent.os.environ, {"HEYSURE_SERVER": "https://custom.example"}, clear=True):
            self.assertEqual(agent._default_config()["server"], "https://custom.example")

    def test_heysure_server_url_accepts_host_and_adds_http_scheme(self):
        self.assertEqual(agent._server_url("example.com:3000/"), "http://example.com:3000")

    def test_heysure_server_url_rejects_invalid_scheme(self):
        with self.assertRaisesRegex(ValueError, "HeySure 服务器地址无效"):
            agent._server_url("ftp://example.com")

    def test_each_platform_has_independent_management_config(self):
        config = agent._default_config()
        self.assertEqual(set(config["profiles"]), {"grok", "antigravity"})
        self.assertEqual(config["gatewayPort"], 8140)
        for platform in agent.PLATFORMS:
            profile = config["profiles"][platform]
            self.assertIn("platformEnabled", profile)
            self.assertIn("timeoutSeconds", profile)
            self.assertIn("proxyUrl", profile)
        self.assertIn("xaiApiKey", config["profiles"]["grok"])
        self.assertNotIn("sandbox", config["profiles"]["grok"])
        self.assertIn("backend", config["profiles"]["antigravity"])
        self.assertNotIn("xaiApiKey", config["profiles"]["antigravity"])

    def test_hidden_platform_fields_are_not_validated_or_saved(self):
        defaults = agent._default_config()
        grok = agent._normalized_profile("grok", {"backend": ""}, defaults["profiles"]["grok"])
        antigravity = agent._normalized_profile(
            "antigravity", {"backend": ""}, defaults["profiles"]["antigravity"]
        )
        self.assertNotIn("backend", grok)
        self.assertEqual(antigravity["backend"], "cli")

    def test_unified_external_listener_requires_api_key(self):
        defaults = agent._default_config()
        raw = {"server": "http://localhost:3000", "account": "user", "gatewayHost": "0.0.0.0"}
        with self.assertRaises(ValueError):
            agent._normalize_config(raw, defaults)
        configured = agent._normalize_config({**raw, "gatewayApiKey": "secret"}, defaults)
        self.assertEqual(configured["gatewayHost"], "0.0.0.0")
        self.assertEqual(configured["gatewayApiKey"], "secret")

    def test_public_host_is_display_only_and_rejects_urls(self):
        defaults = agent._default_config()
        configured = agent._normalize_config(
            {"server": "http://localhost:3000", "gatewayPublicHost": "api.example.com"}, defaults
        )
        self.assertEqual(configured["gatewayPublicHost"], "api.example.com")
        with self.assertRaises(ValueError):
            agent._normalize_config(
                {"server": "http://localhost:3000", "gatewayPublicHost": "https://api.example.com/v1"}, defaults
            )

    def test_blank_numeric_fields_keep_platform_defaults(self):
        defaults = agent._default_config()
        blank_numbers = {
            "timeoutSeconds": "", "toolGraceSeconds": "", "sessionTtlSeconds": "",
            "maxSessions": "", "argSafeBytes": "", "callbackPort": "",
        }
        raw = {
            "server": "http://localhost:3000", "gatewayPort": "",
            "profiles": {name: dict(blank_numbers) for name in agent.PLATFORMS},
        }
        configured = agent._normalize_config(raw, defaults)
        self.assertEqual(configured["gatewayPort"], 8140)
        self.assertEqual(configured["profiles"]["grok"]["timeoutSeconds"], 600)
        self.assertEqual(configured["profiles"]["grok"]["toolGraceSeconds"], 0.5)
        self.assertEqual(configured["profiles"]["antigravity"]["callbackPort"], 51121)

    def test_invalid_numeric_field_has_readable_error(self):
        defaults = agent._default_config()
        with self.assertRaisesRegex(ValueError, "工具收集窗口必须是有效数字"):
            agent._normalize_config(
                {"server": "http://localhost:3000", "profiles": {"grok": {"toolGraceSeconds": "abc"}}},
                defaults,
            )

    def test_partial_config_updates_preserve_other_sections(self):
        previous = agent._default_config()
        previous.update({"server": "http://old:3000", "account": "old-user", "gatewayPort": 8140})
        previous["profiles"]["antigravity"]["workspace"] = "/saved/antigravity"
        gateway_only = agent._normalize_config({"gatewayPort": 9000}, previous)
        self.assertEqual(gateway_only["server"], "http://old:3000")
        self.assertEqual(gateway_only["profiles"]["antigravity"]["workspace"], "/saved/antigravity")
        cli_only = agent._normalize_config(
            {"platform": "grok", "profiles": {"grok": {"timeoutSeconds": 321}}}, gateway_only
        )
        self.assertEqual(cli_only["gatewayPort"], 9000)
        self.assertEqual(cli_only["profiles"]["grok"]["timeoutSeconds"], 321)
        self.assertEqual(cli_only["profiles"]["antigravity"]["workspace"], "/saved/antigravity")

    def test_retired_codex_defaults_migrate_to_grok(self):
        previous = agent._default_config()
        previous["platform"] = "codex"
        previous["defaultPlatform"] = "codex"

        configured = agent._normalize_config({}, previous)

        self.assertEqual(configured["platform"], "grok")
        self.assertEqual(configured["defaultPlatform"], "grok")

    def test_all_platforms_offer_management_jobs(self):
        config = agent._default_config()
        jobs = agent.ManagementJob()
        for platform in agent.PLATFORMS:
            for action in ("deps", "install-cli", "login", "login-status"):
                self.assertTrue(jobs._command(platform, action, config["profiles"][platform]))

    def test_private_gateway_uses_loopback_without_api_key(self):
        profile = agent._default_config()["profiles"]["grok"]
        fleet = agent.UnifiedGatewayFleet()
        active = fleet._private_config("grok", {"enabled": True, "profiles": {"grok": profile}})
        argv = agent.GatewayManager()._argv(active, 8100)
        self.assertIn("127.0.0.1", argv)
        self.assertIn("8100", argv)
        env = agent.GatewayManager()._child_env(active)
        self.assertNotIn("GROK_CLI_API_KEY", env)

    def test_gateway_manager_uses_unified_entrypoint(self):
        profile = agent._default_config()["profiles"]["grok"]
        argv = agent.GatewayManager()._argv(
            {**profile, "platform": "grok", "enabled": True, "configured": True}, 8100
        )
        self.assertTrue(argv[1].endswith("cli_to_api\\server.py") or argv[1].endswith("cli_to_api/server.py"))
        self.assertEqual(argv[2:4], ["--platform", "grok"])


class _FakeManager:
    def __init__(self, platform):
        self.platform = platform
        self.calls = []

    def status(self):
        return {"ready": True, "enabled": True, "recentLogs": []}

    def request(self, path, payload=None, timeout=None, headers=None):
        self.calls.append((path, payload))
        if path == "/v1/models":
            return {"object": "list", "data": [{"id": f"{self.platform}-model", "object": "model"}]}
        return {"choices": [], "model": payload.get("model", "")}


class UnifiedRoutingTests(unittest.TestCase):
    def setUp(self):
        self.fleet = agent.UnifiedGatewayFleet()
        self.fleet.managers = {name: _FakeManager(name) for name in agent.PLATFORMS}
        self.fleet._config = agent._default_config()
        self.fleet._config["profiles"]["grok"]["models"] = "grok-4.5,gpt-5"
        self.fleet._config["profiles"]["antigravity"]["models"] = "gemini-3.5-flash"

    def test_routes_by_configured_model_and_prefix(self):
        self.assertEqual(self.fleet.complete({"model": "grok-4.5"})["cli_platform"], "grok")
        self.assertEqual(self.fleet.complete({"model": "gemini-new"})["cli_platform"], "antigravity")
        self.assertEqual(self.fleet.complete({"model": "gpt-5"})["cli_platform"], "grok")

    def test_explicit_platform_override_is_removed_before_forwarding(self):
        result = self.fleet.complete({"model": "gpt-5", "cli_platform": "grok"})
        self.assertEqual(result["cli_platform"], "grok")
        forwarded = self.fleet.managers["grok"].calls[-1][1]
        self.assertNotIn("cli_platform", forwarded)
        with self.assertRaises(ValueError):
            self.fleet.complete({"model": "gpt-5", "cli_platform": "unknown"})

    def test_models_are_aggregated_and_tagged(self):
        result = self.fleet.models()
        self.assertEqual(len(result["data"]), 2)
        self.assertEqual({item["cli_platform"] for item in result["data"]}, set(agent.PLATFORMS))

    def test_unified_http_api_uses_one_key_and_aggregates_models(self):
        port = agent._free_port()
        self.fleet._config.update({"enabled": True, "gatewayHost": "127.0.0.1", "gatewayPort": port, "gatewayApiKey": "secret"})
        self.fleet._start_api(self.fleet._config)
        try:
            with self.assertRaisesRegex(RuntimeError, "HTTP 401"):
                agent._json_request(f"http://127.0.0.1:{port}/v1/models")
            result = agent._json_request(
                f"http://127.0.0.1:{port}/v1/models",
                headers={"Authorization": "Bearer secret"},
            )
            self.assertEqual(len(result["data"]), 2)
            completion = agent._json_request(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                {"model": "grok-4.5", "messages": [{"role": "user", "content": "hi"}]},
                headers={"Authorization": "Bearer secret"},
            )
            self.assertEqual(completion["cli_platform"], "grok")
        finally:
            self.fleet._stop_api()


class ControlActionTests(unittest.TestCase):
    def test_heysure_login_action_returns_synchronous_result(self):
        expected = {"authenticated": True, "socketUrl": "http://example.test:3000"}
        app = agent.ControlApp.__new__(agent.ControlApp)
        app.adapter = SimpleNamespace(verify_login=lambda: expected)
        self.assertEqual(app.action("heysure-login", {}), expected)

    def test_generated_api_keys_are_random_and_prefixed(self):
        app = agent.ControlApp.__new__(agent.ControlApp)
        first = app.action("generate-api-key", {})["apiKey"]
        second = app.action("generate-api-key", {})["apiKey"]
        self.assertTrue(first.startswith("hs_"))
        self.assertGreaterEqual(len(first), 40)
        self.assertNotEqual(first, second)

    def test_cli_config_detection_finds_command_models_and_directories(self):
        class Manager:
            def status(self):
                return {"ready": False}

        class Fleet:
            managers = {name: Manager() for name in agent.PLATFORMS}

            def _child_env(self, config):
                return {}

        app = agent.ControlApp.__new__(agent.ControlApp)
        app.config = agent._default_config()
        app.adapter = SimpleNamespace(gateway=Fleet())

        def completed(argv, **kwargs):
            return agent.subprocess.CompletedProcess(argv, 0, stdout="grok 1.2.3", stderr="")

        with patch.object(agent.shutil, "which", return_value="C:/tools/grok.exe"), patch.object(
            agent.subprocess, "run", side_effect=completed
        ):
            result = app.detect_cli_config("grok")
        self.assertEqual(result["values"]["command"], "C:/tools/grok.exe")
        self.assertEqual(result["values"]["model"], "grok-4.5")
        self.assertEqual(result["values"]["models"], "grok-4.5")
        self.assertEqual(result["values"]["timeoutSeconds"], 600)
        self.assertTrue(result["values"]["sessionsDir"].endswith("sessions\\grok") or result["values"]["sessionsDir"].endswith("sessions/grok"))

    def test_service_start_stop_are_global_and_preserve_child_selection(self):
        class Gateway:
            def __init__(self):
                self.applied = []

            def status(self):
                return {"ready": False, "recentLogs": [], "platforms": {}}

        class Adapter:
            def __init__(self):
                self.gateway = Gateway()

            def apply_config(self, config):
                self.gateway.applied.append(config["enabled"])

        app = agent.ControlApp.__new__(agent.ControlApp)
        app.config = agent._default_config()
        app.config["profiles"]["grok"]["platformEnabled"] = True
        app.config["profiles"]["antigravity"]["platformEnabled"] = False
        app.adapter = Adapter()
        with patch.object(agent, "_save_config"):
            app.action("start", {})
            self.assertTrue(app.config["enabled"])
            app.action("stop", {})
            self.assertFalse(app.config["enabled"])
        self.assertTrue(app.config["profiles"]["grok"]["platformEnabled"])
        self.assertFalse(app.config["profiles"]["antigravity"]["platformEnabled"])
        self.assertEqual(app.adapter.gateway.applied, [True, False])


if __name__ == "__main__":
    unittest.main()
