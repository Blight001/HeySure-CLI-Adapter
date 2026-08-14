import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

CLI_ROOT = Path(__file__).resolve().parents[1]
if str(CLI_ROOT) not in sys.path:
    sys.path.insert(0, str(CLI_ROOT))

from cli_gateway.backends import grok_acp as acp_bridge
from cli_gateway.backends import grok as server


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

    def test_acp_ignores_loaded_history_outside_active_turn(self):
        sess = self.make_session()
        old_update = {
            "method": "session/update",
            "params": {"update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "previous answer"},
            }},
        }
        current_update = {
            "method": "session/update",
            "params": {"update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "current answer"},
            }},
        }

        # session/load history replay happens before session/prompt.
        sess._handle_notification(old_update)
        self.assertTrue(sess.queue.empty())

        with mock.patch.object(sess, "_rpc_send", return_value=7):
            sess.start_turn("new question")
        sess._handle_notification(current_update)
        self.assertEqual(sess.queue.get_nowait(), ("text", "current answer"))

        sess._handle_response({"id": 7, "result": {"stopReason": "end_turn"}})
        self.assertEqual(sess.queue.get_nowait(), ("end", "end_turn"))
        sess._handle_notification(old_update)
        self.assertTrue(sess.queue.empty())

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

    def test_shortened_persisted_history_is_treated_as_rewrite(self):
        identity = "recall-session"
        original = dict(server._ACP_PERSISTED)
        try:
            server._ACP_PERSISTED.clear()
            server._ACP_PERSISTED[identity] = {
                "session_id": "native-session",
                "message_hashes": ["one", "two", "three"],
            }
            self.assertFalse(server._persisted_history_rewritten(
                identity, ["one", "two", "three", "four"], False
            ))
            self.assertTrue(server._persisted_history_rewritten(
                identity, ["one"], False
            ))
            self.assertFalse(server._persisted_history_rewritten(
                identity, ["one"], True
            ))
        finally:
            server._ACP_PERSISTED.clear()
            server._ACP_PERSISTED.update(original)

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

    def test_uploaded_history_recovers_identity_without_header(self):
        initial = [
            {"role": "system", "content": "stable system"},
            {"role": "user", "content": "first"},
        ]
        response = {"role": "assistant", "content": "first answer"}
        follow_up = initial + [response, {"role": "user", "content": "second"}]
        with server._ACP_PERSISTED_LOCK:
            previous = dict(server._ACP_PERSISTED)
            server._ACP_PERSISTED.clear()
        try:
            with mock.patch.object(server, "_save_persisted_sessions_locked"):
                identity = server._resolve_session_identity("", initial, "grok-4.5")
                synced = server._message_hashes(initial) + [server._fingerprint(response)]
                server._remember_persisted_session(
                    identity, "grok-session", model="grok-4.5",
                    message_hashes=synced,
                )
                self.assertEqual(
                    server._resolve_session_identity("", follow_up, "grok-4.5"),
                    identity,
                )
                self.assertNotEqual(
                    server._resolve_session_identity("", initial, "grok-4.5"),
                    identity,
                )
        finally:
            with server._ACP_PERSISTED_LOCK:
                server._ACP_PERSISTED.clear()
                server._ACP_PERSISTED.update(previous)

    def test_explicit_identity_has_priority_over_uploaded_history(self):
        self.assertEqual(
            server._resolve_session_identity(
                "heysure-stable", [{"role": "user", "content": "x"}], "grok-4.5"
            ),
            "heysure-stable",
        )

    def test_tool_result_loads_same_persisted_grok_session(self):
        registry = acp_bridge.SessionRegistry(ttl=60, max_sessions=2)
        handler = object.__new__(server.Handler)
        created = []

        def fake_create(**kwargs):
            sess = self.make_session()
            sess.session_id = kwargs.get("resume_session_id") or "new-session"
            sess.prompts = []
            sess.start_turn = sess.prompts.append
            kwargs["registry"].add(sess)
            created.append((sess, kwargs))
            return sess

        messages = [
            {"role": "user", "content": "send it"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_deadbeef-1",
                    "type": "function",
                    "function": {"name": "demo", "arguments": "{}"},
                }],
            },
            {"role": "tool", "tool_call_id": "call_deadbeef-1", "content": "ok"},
        ]
        with mock.patch.object(server, "ACP_REGISTRY", registry), mock.patch.object(
            server, "_persisted_session", return_value="same-native-session"
        ), mock.patch.object(
            server, "_resolve_cli_argv", return_value=["grok"]
        ), mock.patch.object(
            acp_bridge.AcpSession, "create", side_effect=fake_create
        ), mock.patch.object(
            server.Handler, "_acp_blocking", lambda *_args: None
        ):
            handler._handle_acp_chat(
                "grok-4.5", messages, [], False, "preview", "stable-chat", None
            )

        sess, kwargs = created[0]
        self.assertEqual(kwargs["resume_session_id"], "same-native-session")
        self.assertEqual(sess.session_id, "same-native-session")
        self.assertIn("call_deadbeef-1: ok", sess.prompts[0])
        registry.drop(sess)

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


class AlwaysAcpRoutingTest(unittest.TestCase):
    def test_request_without_tools_still_uses_acp(self):
        payload = {
            "model": "grok-4.5",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": False,
        }
        body = json.dumps(payload).encode("utf-8")
        handler = object.__new__(server.Handler)
        handler.headers = {"Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        handler._path = lambda: "/v1/chat/completions"
        handler._check_auth = lambda: True
        handler._handle_acp_chat = mock.Mock()
        handler._handle_stream = mock.Mock(side_effect=AssertionError("headless stream used"))
        handler._handle_blocking = mock.Mock(side_effect=AssertionError("headless blocking used"))

        handler.do_POST()

        handler._handle_acp_chat.assert_called_once()
        args = handler._handle_acp_chat.call_args.args
        self.assertEqual(args[2], [])
        self.assertTrue(args[5].startswith("history-"))


class HandlerDisconnectTest(unittest.TestCase):
    def test_mcp_client_disconnect_does_not_escape_request_handler(self):
        handler = object.__new__(server.Handler)
        handler.close_connection = False

        with mock.patch.object(handler, "_path", return_value="/mcp/1234abcd"), mock.patch.object(
            handler, "_handle_mcp", side_effect=BrokenPipeError(32, "Broken pipe")
        ) as handle_mcp:
            handler.do_POST()

        handle_mcp.assert_called_once_with("1234abcd")
        self.assertTrue(handler.close_connection)


if __name__ == "__main__":
    unittest.main()
