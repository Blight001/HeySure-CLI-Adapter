import unittest
from unittest import mock

import controller


class _Client:
    def __init__(self):
        self.calls = []

    def call(self, name, arguments=None):
        self.calls.append((name, arguments or {}))
        if name == "heysure.claim_message":
            return {"message": {
                "turn_id": "xturn-1",
                "session_id": "session-1",
                "history": [{"role": "user", "content": "你好"}],
            }}
        return {"ok": True}


class ControllerTests(unittest.TestCase):
    def test_run_once_claims_and_replies(self):
        client = _Client()
        gateway = mock.Mock()
        gateway.complete.return_value = {
            "model": "codex-default",
            "choices": [{"message": {"content": "你好，我在。"}}],
        }
        context = {"member": {"id": 19, "prompt": "你是德克萨斯"}}

        self.assertTrue(controller.run_once(client, gateway, context))

        payload = gateway.complete.call_args.args[0]
        self.assertTrue(payload["_heysure_native_mcp"])
        self.assertEqual(payload["_heysure_session_id"], "heysure:19:session-1")
        self.assertEqual(client.calls[-1][0], "heysure.reply_message")
        self.assertEqual(client.calls[-1][1]["content"], "你好，我在。")

    def test_run_once_reports_gateway_failure_to_turn(self):
        client = _Client()
        gateway = mock.Mock()
        gateway.complete.side_effect = RuntimeError("codex unavailable")

        with self.assertRaises(RuntimeError):
            controller.run_once(client, gateway, {"member": {"id": 19}})

        self.assertEqual(client.calls[-1][0], "heysure.fail_message")
        self.assertIn("codex unavailable", client.calls[-1][1]["error"])


if __name__ == "__main__":
    unittest.main()
