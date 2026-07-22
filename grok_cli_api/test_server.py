import unittest
from unittest import mock

import acp_bridge
import server


class StatefulAcpHelpersTest(unittest.TestCase):
    def make_session(self):
        return acp_bridge.AcpSession("1234abcd", "grok-4.5")

    def remember(self, sess, request_messages, response_text="first answer"):
        server.Handler._remember_acp_response(
            sess,
            {
                "digest": "request-digest",
                "message_hashes": server._message_hashes(request_messages),
            },
            {"role": "assistant", "content": response_text},
            "stop",
        )

    def test_append_only_history_resumes_after_previous_answer(self):
        sess = self.make_session()
        initial = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "first"},
        ]
        self.remember(sess, initial)
        follow_up = initial + [
            {"role": "assistant", "content": "first answer"},
            {"role": "user", "content": "second"},
        ]
        self.assertEqual(server._resume_message_index(sess, follow_up), 3)
        prompt = server._incremental_prompt(follow_up[3:], [])
        self.assertIn("second", prompt)
        self.assertNotIn("first answer", prompt)

    def test_trimmed_previous_answer_is_accepted(self):
        sess = self.make_session()
        initial = [{"role": "user", "content": "first"}]
        self.remember(sess, initial, "kept text and trimmed suffix")
        follow_up = initial + [
            {"role": "assistant", "content": "kept text"},
            {"role": "user", "content": "second"},
        ]
        self.assertEqual(server._resume_message_index(sess, follow_up), 2)

    def test_rewritten_history_requires_new_session(self):
        sess = self.make_session()
        initial = [{"role": "user", "content": "first"}]
        self.remember(sess, initial)
        rewritten = [{"role": "user", "content": "compressed context"}]
        self.assertIsNone(server._resume_message_index(sess, rewritten))

    def test_registry_identity_is_removed_when_session_drops(self):
        registry = acp_bridge.SessionRegistry(ttl=60, max_sessions=2)
        sess = self.make_session()
        registry.add(sess)
        registry.bind_identity(sess, "heysure-stable-id")
        self.assertIs(registry.get_by_identity("heysure-stable-id"), sess)
        registry.drop(sess)
        self.assertIsNone(registry.get_by_identity("heysure-stable-id"))
        self.assertTrue(sess.closed)

    def test_rebinding_identity_closes_old_session(self):
        registry = acp_bridge.SessionRegistry(ttl=60, max_sessions=2)
        old = self.make_session()
        new = acp_bridge.AcpSession("8765dcba", "grok-4.5")
        registry.add(old)
        registry.bind_identity(old, "same-chat")
        registry.add(new)
        registry.bind_identity(new, "same-chat")
        self.assertTrue(old.closed)
        self.assertIs(registry.get_by_identity("same-chat"), new)
        registry.drop(new)

    def test_handler_reuses_live_acp_and_sends_only_delta(self):
        registry = acp_bridge.SessionRegistry(ttl=60, max_sessions=2)
        handler = object.__new__(server.Handler)
        created = []
        cached_responses = []
        handler._json_response = lambda _status, payload: cached_responses.append(payload)

        def fake_create(**kwargs):
            sess = self.make_session()
            sess.prompts = []
            sess.start_turn = sess.prompts.append
            kwargs["registry"].add(sess)
            created.append(sess)
            return sess

        def fake_blocking(this, sess, _model, _preview, request_state):
            answer = "first answer" if len(sess.prompts) == 1 else "second answer"
            this._remember_acp_response(
                sess,
                request_state,
                {"role": "assistant", "content": answer},
                "stop",
            )
            this._acp_park(sess)

        initial = [{"role": "user", "content": "first"}]
        follow_up = initial + [
            {"role": "assistant", "content": "first answer"},
            {"role": "user", "content": "second"},
        ]
        tools = [{
            "type": "function",
            "function": {
                "name": "demo",
                "description": "demo",
                "parameters": {"type": "object", "properties": {}},
            },
        }]
        with mock.patch.object(server, "ACP_REGISTRY", registry), mock.patch.object(
            server, "_resolve_cli_argv", return_value=["grok"]
        ), mock.patch.object(
            acp_bridge.AcpSession, "create", side_effect=fake_create
        ), mock.patch.object(
            server.Handler, "_acp_blocking", fake_blocking
        ):
            handler._handle_acp_chat(
                "grok-4.5", initial, tools, False, "initial", "stable-chat", None
            )
            handler._handle_acp_chat(
                "grok-4.5", follow_up, tools, False, "follow", "stable-chat", None
            )
            # An HTTP retry of the same second-turn request must not prompt grok
            # again; it should replay the response remembered above.
            handler._handle_acp_chat(
                "grok-4.5", follow_up, tools, False, "follow", "stable-chat", None
            )

        self.assertEqual(len(created), 1)
        self.assertEqual(len(created[0].prompts), 2)
        self.assertIn("first", created[0].prompts[0])
        self.assertIn("second", created[0].prompts[1])
        self.assertNotIn("first answer", created[0].prompts[1])
        self.assertEqual(len(cached_responses), 1)
        self.assertTrue(cached_responses[0]["cached"])
        self.assertEqual(
            cached_responses[0]["choices"][0]["message"]["content"],
            "second answer",
        )
        registry.drop(created[0])


if __name__ == "__main__":
    unittest.main()
