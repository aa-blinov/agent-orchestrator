import contextlib
import io
import json
import os
import sqlite3
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
        self.assertIn("--standalone", command)
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

    def test_usage_ledger_groups_calls_without_storing_prompts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "usage.sqlite3")
            with mock.patch.object(bridge, "USAGE_DB", path):
                bridge.store_usage("task-demo", "codex", "gpt-6-luna", "implement", 0, False, 2.5,
                                   {"input_tokens": 100, "output_tokens": 20, "cached_input_tokens": 60})
                bridge.store_usage("task-demo", "claude", "sonnet", "research", 0, False, 1.0,
                                   {"input_tokens": 50, "output_tokens": 10, "total_tokens": 60})
                detail = bridge.usage_report({"task_id": "task-demo"})
                recent = bridge.usage_report({"limit": 5})
            self.assertIn("2 calls", detail)
            self.assertIn("codex / gpt-6-luna: 1 calls, 1 with usage reported", detail)
            self.assertIn("input 100 (1/1)", detail)
            self.assertIn("claude / sonnet: 1 calls, 1 with usage reported", detail)
            self.assertIn("total 60 (1/1)", detail)
            self.assertNotIn("150", detail)
            self.assertIn("each provider reports them", detail)
            self.assertNotIn("subset", detail)
            self.assertIn("task-demo", recent)
            self.assertIn("  - codex / gpt-6-luna", recent)
            with sqlite3.connect(path) as database:
                columns = {row[1] for row in database.execute("PRAGMA table_info(agent_calls)")}
            self.assertNotIn("prompt", columns)
            self.assertNotIn("response", columns)


class UsageGroupingTests(unittest.TestCase):
    def store(self, path, task_id, agent, model, usage):
        with mock.patch.object(bridge, "USAGE_DB", path):
            bridge.store_usage(task_id, agent, model, "implement", 0, False, 1.0, usage)

    def test_mixed_agents_are_never_summed_together(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "usage.sqlite3")
            self.store(path, "task-mix", "codex", "gpt-a", {"input_tokens": 100, "cached_input_tokens": 80})
            self.store(path, "task-mix", "claude", "sonnet", {"input_tokens": 50, "cache_write_tokens": 7})
            with mock.patch.object(bridge, "USAGE_DB", path):
                detail = bridge.usage_report({"task_id": "task-mix"})
                recent = bridge.usage_report({"limit": 5})
        for text in (detail, recent):
            self.assertIn("codex / gpt-a: 1 calls, 1 with usage reported | input 100 (1/1), output unavailable (0/1), cached input 80 (1/1)", text)
            self.assertIn("claude / sonnet: 1 calls, 1 with usage reported | input 50 (1/1), output unavailable (0/1), cached input unavailable (0/1), cache write 7 (1/1)", text)
            self.assertNotIn("150", text)

    def test_same_agent_with_different_models_is_split(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "usage.sqlite3")
            self.store(path, "task-models", "codex", "gpt-research", {"input_tokens": 10})
            self.store(path, "task-models", "codex", "gpt-implement", {"input_tokens": 20})
            with mock.patch.object(bridge, "USAGE_DB", path):
                detail = bridge.usage_report({"task_id": "task-models"})
        self.assertIn("codex / gpt-research: 1 calls, 1 with usage reported | input 10 (1/1)", detail)
        self.assertIn("codex / gpt-implement: 1 calls, 1 with usage reported | input 20 (1/1)", detail)
        self.assertNotIn("input 30", detail)

    def test_missing_fields_stay_unavailable_while_zero_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "usage.sqlite3")
            self.store(path, "task-zero", "agy", "model-x", {"input_tokens": 0, "output_tokens": 5})
            self.store(path, "task-zero", "agy", "model-x", {"output_tokens": 3})
            with mock.patch.object(bridge, "USAGE_DB", path):
                detail = bridge.usage_report({"task_id": "task-zero"})
                recent = bridge.usage_report({"limit": 5})
        line = "agy / model-x: 2 calls, 2 with usage reported | input 0 (1/2), output 8 (2/2), cached input unavailable (0/2)"
        self.assertIn(line, detail)
        self.assertIn("  - " + line, recent)
        self.assertIn("reasoning unavailable (0/2)", detail)

    def test_recent_summary_lists_each_task_and_respects_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "usage.sqlite3")
            self.store(path, "task-old", "codex", "gpt-a", {"input_tokens": 1})
            self.store(path, "task-new", "codex", "gpt-a", {"input_tokens": 2})
            self.store(path, "task-new", "claude", "sonnet", {"input_tokens": 3})
            with mock.patch.object(bridge, "USAGE_DB", path):
                recent = bridge.usage_report({"limit": 5})
                latest_only = bridge.usage_report({"limit": 1})
        self.assertLess(recent.index("task_id=task-new"), recent.index("task_id=task-old"))
        self.assertIn("  - codex / gpt-a: 1 calls, 1 with usage reported | input 2 (1/1)", recent)
        self.assertIn("  - claude / sonnet: 1 calls, 1 with usage reported | input 3 (1/1)", recent)
        self.assertIn("task_id=task-new", latest_only)
        self.assertNotIn("task-old", latest_only)


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
