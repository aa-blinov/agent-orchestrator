import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from src import agent_orchestrator as bridge


class CommandTests(unittest.TestCase):
    def test_codex_uses_separate_models_and_restricted_modes(self):
        agents = dict(bridge.AGENTS)
        agents["codex"] = dict(agents["codex"], preferred_models={"research": "gpt-6.1-terra", "implement": "gpt-6-luna"})
        with mock.patch.object(bridge, "AGENTS", agents):
            with mock.patch.object(bridge, "PROFILES", {"native": {"models": {}}}):
                with mock.patch.object(bridge, "agent_binary", return_value="/bin/codex"):
                    research = bridge.cli_command("codex", "inspect this", "research")
                    implementation = bridge.cli_command("codex", "edit this", "implement")
        self.assertEqual(research, ["/bin/codex", "exec", "--ephemeral", "--sandbox", "read-only", "--skip-git-repo-check", "--json", "--model", "gpt-6.1-terra", "--", "inspect this"])
        self.assertEqual(implementation, ["/bin/codex", "exec", "--ephemeral", "--sandbox", "workspace-write", "--skip-git-repo-check", "--json", "--model", "gpt-6-luna", "--", "edit this"])

    def test_agy_receives_prompt_as_print_value(self):
        agents = dict(bridge.AGENTS)
        agents["agy"] = {key: value for key, value in agents["agy"].items() if key != "preferred_models"}
        with mock.patch.object(bridge, "AGENTS", agents):
            with mock.patch.object(bridge, "agent_binary", return_value="/bin/agy"):
                command = bridge.cli_command("agy", "--review this", "research")
        self.assertEqual(command, ["/bin/agy", "--mode", "plan", "--output-format", "json", "--print=--review this"])

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

    def test_profile_model_is_added_to_native_command(self):
        agents = dict(bridge.AGENTS)
        agents["claude"] = dict(agents["claude"], preferred_models={"research": "haiku", "implement": "sonnet"})
        with mock.patch.object(bridge, "AGENTS", agents):
            with mock.patch.object(bridge, "PROFILES", {"native": {"models": {}}, "strong": {"models": {"claude": "opus"}}}):
                with mock.patch.object(bridge, "agent_binary", return_value="/bin/claude"):
                    command = bridge.cli_command("claude", "review this", "research", "strong")
                    default_command = bridge.cli_command("claude", "review this", "research")
        self.assertIn("opus", command)
        self.assertNotIn("haiku", command)
        self.assertIn("haiku", default_command)

    def test_agent_can_choose_a_different_model_for_implementation(self):
        agents = dict(bridge.AGENTS)
        agents["claude"] = dict(agents["claude"], preferred_models={"research": "opus", "implement": "sonnet"})
        with mock.patch.object(bridge, "AGENTS", agents):
            with mock.patch.object(bridge, "PROFILES", {"native": {"models": {}}}):
                with mock.patch.object(bridge, "agent_binary", return_value="/bin/claude"):
                    research = bridge.cli_command("claude", "task", "research")
                    implementation = bridge.cli_command("claude", "task", "implement")
        self.assertIn("opus", research)
        self.assertIn("sonnet", implementation)

    def test_config_can_register_a_generic_agent(self):
        config = {
            "default_profile": "native",
            "profiles": {"native": {"description": "CLI default", "models": {}}},
            "agents": {
                "my-agent": {
                    "command": "my-agent", "label": "My agent",
                    "args": {"research": ["--plan"], "implement": ["--edit"]},
                    "task_args": {"research": ["--prompt", "{task}"], "implement": ["--prompt", "{task}"]},
                    "model_args": ["--model", "{model}"], "preferred_models": {"research": "model-x"},
                }
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "config.json")
            with open(path, "w", encoding="utf-8") as output:
                json.dump(config, output)
            agents, profiles, default = bridge.load_settings(path)
        self.assertEqual(default, "native")
        self.assertEqual(agents["my-agent"]["preferred_models"]["research"], "model-x")
        self.assertIn("native", profiles)

    def test_custom_agent_command_template_uses_separate_arguments(self):
        spec = {
            "command": "my-agent", "label": "My agent", "preferred_model": "model-x",
            "model_args": ["--model", "{model}"],
            "args": {"research": ["--plan"], "implement": ["--edit"]},
            "task_args": {"research": ["--prompt", "{task}"], "implement": ["--prompt", "{task}"]},
        }
        with mock.patch.dict(bridge.AGENTS, {"my-agent": spec}):
            with mock.patch.object(bridge, "agent_binary", return_value="/bin/my-agent"):
                command = bridge.cli_command("my-agent", "task with spaces", "implement")
        self.assertEqual(command, ["/bin/my-agent", "--edit", "--model", "model-x", "--prompt", "task with spaces"])


class UsageTests(unittest.TestCase):
    def test_codex_json_stream_reports_usage_and_final_answer(self):
        raw = '\n'.join([
            '{"type":"item.completed","item":{"type":"agent_message","text":"READY"}}',
            '{"type":"turn.completed","usage":{"input_tokens":120,"cached_input_tokens":80,"output_tokens":15}}',
        ])
        answer, usage = bridge.extract_usage("codex", raw)
        self.assertEqual(answer, "READY")
        self.assertEqual(usage, {"input_tokens": 120, "output_tokens": 15, "cached_input_tokens": 80})

    def test_claude_and_agy_json_usage(self):
        claude, claude_usage = bridge.extract_usage("claude", '{"result":"Done","usage":{"input_tokens":10,"output_tokens":4,"cache_read_input_tokens":7}}')
        agy, agy_usage = bridge.extract_usage("agy", '{"response":"Done","usage":{"input_tokens":10,"output_tokens":4,"thinking_tokens":2,"total_tokens":14}}')
        self.assertEqual((claude, claude_usage["cached_input_tokens"]), ("Done", 7))
        self.assertEqual((agy, agy_usage["reasoning_tokens"], agy_usage["total_tokens"]), ("Done", 2, 14))

    def test_opencode_step_usage_and_missing_usage_label(self):
        raw = '{"type":"step_finish","part":{"tokens":{"input":30,"output":8,"reasoning":3,"cache":{"read":20,"write":1}}}}'
        _, usage = bridge.extract_usage("opencode", raw)
        self.assertEqual(usage["input_tokens"], 30)
        self.assertEqual(usage["cached_input_tokens"], 20)
        self.assertEqual(usage["reasoning_tokens"], 3)
        self.assertIn("not reported", bridge.usage_summary(None))


class ProtocolTests(unittest.TestCase):
    def call(self, request):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            bridge.handle(request)
        return [json.loads(line) for line in output.getvalue().splitlines()]

    def test_initialize_advertises_server(self):
        response = self.call({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})[0]
        self.assertEqual(response["result"]["serverInfo"]["name"], "agent-orchestrator")

    def test_invalid_tool_name_returns_json_rpc_error(self):
        response = self.call({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": []}})[0]
        self.assertEqual(response["error"]["code"], -32602)

    def test_notification_has_no_response(self):
        response = self.call({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual(response, [])

    def test_host_orchestrator_is_excluded_from_tools(self):
        with mock.patch.object(bridge, "ORCHESTRATOR", "claude"):
            tools = bridge.tools_for_host()
        delegate = next(item for item in tools if item["name"] == "delegate_agent")
        self.assertNotIn("claude", delegate["inputSchema"]["properties"]["agent"]["enum"])
        self.assertIn("agy", delegate["inputSchema"]["properties"]["agent"]["enum"])
        self.assertIn("profile", delegate["inputSchema"]["properties"])
        self.assertIn("native", delegate["inputSchema"]["properties"]["profile"]["enum"])

    def test_codex_is_peer_but_excluded_when_it_is_the_host(self):
        with mock.patch.object(bridge, "ORCHESTRATOR", "claude"):
            claude_tools = bridge.tools_for_host()
        delegate = next(item for item in claude_tools if item["name"] == "delegate_agent")
        self.assertIn("codex", delegate["inputSchema"]["properties"]["agent"]["enum"])
        with mock.patch.object(bridge, "ORCHESTRATOR", "codex"):
            codex_tools = bridge.tools_for_host()
        delegate = next(item for item in codex_tools if item["name"] == "delegate_agent")
        self.assertNotIn("codex", delegate["inputSchema"]["properties"]["agent"]["enum"])


if __name__ == "__main__":
    unittest.main()
