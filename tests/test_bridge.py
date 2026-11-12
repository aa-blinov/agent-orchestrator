import contextlib
import io
import json
import unittest
from unittest import mock

from src import codex_agent_bridge as bridge


class CommandTests(unittest.TestCase):
    def test_agy_receives_prompt_as_print_value(self):
        with mock.patch.object(bridge, "agent_binary", return_value="/bin/agy"):
            command = bridge.cli_command("agy", "--review this", "research")
        self.assertEqual(command, ["/bin/agy", "--mode", "plan", "--print=--review this"])

    def test_claude_prompt_is_after_option_separator(self):
        with mock.patch.object(bridge, "agent_binary", return_value="/bin/claude"):
            command = bridge.cli_command("claude", "--review this", "implement")
        self.assertEqual(command[-2:], ["--", "--review this"])
        self.assertIn("acceptEdits", command)

    def test_opencode_implementation_is_explicitly_automatic(self):
        with mock.patch.object(bridge, "agent_binary", return_value="/bin/opencode"):
            command = bridge.cli_command("opencode", "implement task", "implement")
        self.assertIn("--auto", command)
        self.assertEqual(command[-2:], ["--", "implement task"])

    def test_agent_binary_uses_named_override(self):
        with mock.patch.dict(bridge.os.environ, {"CLAUDE_BIN": "/bin/claude"}):
            with mock.patch("os.path.isfile", return_value=True):
                self.assertEqual(bridge.agent_binary("claude"), "/bin/claude")


class ProtocolTests(unittest.TestCase):
    def call(self, request):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            bridge.handle(request)
        return [json.loads(line) for line in output.getvalue().splitlines()]

    def test_initialize_advertises_server(self):
        response = self.call({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})[0]
        self.assertEqual(response["result"]["serverInfo"]["name"], "codex-agent-bridge")

    def test_invalid_tool_name_returns_json_rpc_error(self):
        response = self.call({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": []}})[0]
        self.assertEqual(response["error"]["code"], -32602)

    def test_notification_has_no_response(self):
        response = self.call({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual(response, [])


if __name__ == "__main__":
    unittest.main()
