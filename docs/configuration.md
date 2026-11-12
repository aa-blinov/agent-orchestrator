# Configuration

The bridge reads `~/.config/agent-orchestrator/config.json` at startup. Set `XDG_CONFIG_HOME` to use a different configuration directory. The file is shared by all orchestrator hosts on this machine.

## Model preferences

Give each agent a separate model for `research` (complex analysis and consultation) and `implement` (making changes). Define named profiles for exceptions, then pass the profile name to `delegate_agent` or `consult_agents` when needed:

```json
{
  "default_profile": "balanced",
  "profiles": {
    "balanced": {
      "description": "Use each agent's preferred model",
      "models": {}
    },
    "strong": {
      "description": "Use selected models for harder tasks",
      "models": {
        "agy": "MODEL_SLUG_FROM_AGY",
        "claude": "sonnet",
        "opencode": "provider/model-id"
      }
    }
  },
  "agents": {
    "claude": {"preferred_models": {"research": "opus", "implement": "sonnet"}},
    "opencode": {"preferred_models": {"research": "provider/strong-model", "implement": "provider/fast-model"}}
  }
}
```

Replace the example identifiers with models available in the corresponding CLI. A model in the selected profile takes precedence over the role-specific `preferred_models`; the legacy `preferred_model` is a fallback for both roles. If neither is set, the bridge leaves model selection to the CLI. Codex CLI, Antigravity, and Claude Code accept their model identifier with `--model`; OpenCode uses its provider-qualified model identifier. Codex runs research in read-only mode and implementation with workspace-write access. See the [Codex CLI reference](https://developers.openai.com/codex/cli/reference), [Antigravity CLI guide](https://antigravity.google/docs/cli/headless/), [Claude Code CLI reference](https://docs.anthropic.com/en/docs/claude-code/cli-usage), and [OpenCode model guide](https://opencode.ai/docs/models/).

The bridge requests structured output from each CLI and stores the input, output, cache, and reasoning token counts the CLI reports in `~/.local/share/agent-orchestrator/usage.sqlite3` (or `$XDG_DATA_HOME/agent-orchestrator/usage.sqlite3`). It stores call metadata and counters, not prompts or responses. Use the `usage_report` tool to review recent task totals or details for a `task_id`. Reuse a `task_id` across calls to group them. Totals are per agent and requested model: the report never adds counters across different agents or models, because providers account for cache and reasoning tokens differently. Each counter shows its sum and coverage as `(calls reporting it / calls)`; a missing count (`unavailable`) is not treated as zero, while a reported zero is. Cache counters retain the provider's own categories; the report does not infer a shared relationship between cached and ordinary input. The bridge does not estimate counts or money.

The bridge includes a `native` profile when no configuration file exists. If the file exists, its `default_profile` must name one of the configured profiles. An omitted `profile` uses this default.

Each `delegate_agent` and `consult_agents` call requires a `task_id` and a `session_id`. Reuse one `session_id` for every agent call in a chat, including calls for different tasks in that chat; use a new one in a new chat. The bridge atomically allows at most four launched agent calls and three distinct peer agents per `session_id`. A consultation with two agents uses two call slots. The server cannot read a host chat identifier from standard MCP tool arguments, so the orchestrating agent creates the session ID once and carries it forward. `usage_report` accepts `session_id` to inspect the budget. This is a hard limit per supplied identifier; it cannot authenticate that an identifier corresponds to a particular chat.

## MCP call timeout

The maximum runtime for each consultation or implementation agent call is 7200 seconds. Set the host's MCP tool timeout to at least 7200 seconds as well; otherwise the host may stop waiting before the bridge returns. Configure the per-server timeout in each client:

| Client | Configuration |
| --- | --- |
| Codex | `~/.codex/config.toml`: `[mcp_servers.local-agents]` → `tool_timeout_sec = 7200` |
| Antigravity | `~/.gemini/config/mcp_config.json`: `mcpServers.local-agents.timeoutSeconds = 7200` |
| Claude Code | `~/.claude.json`: `mcpServers.local-agents.timeout = 7200000` (milliseconds) |
| OpenCode | `~/.config/opencode/opencode.jsonc`: `mcp.servers.local-agents.timeout.execution = 7200000` (milliseconds) |

The Codex plugin's bundled MCP server uses the host's 60-second default. For long calls, keep its skill enabled and use the regular MCP registration with `tool_timeout_sec` above; disable the bundled server to avoid duplicate tools.

## Add a CLI agent

Add an entry under `agents`. The bridge builds a subprocess argument list directly; it does not run the configured command through a shell.

```json
{
  "agents": {
    "my-agent": {
      "command": "my-agent",
      "label": "My coding agent",
      "preferred_model": "model-name",
      "preferred_models": {
        "research": "reasoning-model",
        "implement": "coding-model"
      },
      "model_args": ["--model", "{model}"],
      "args": {
        "research": ["--plan"],
        "implement": ["--accept-edits"]
      },
      "task_args": {
        "research": ["--prompt", "{task}"],
        "implement": ["--prompt", "{task}"]
      }
    }
  }
}
```

Required fields are `command`, `label`, `args`, and `task_args`. Both modes are required. `model_args` is optional and is added only when a model is configured. Supported placeholders are `{task}`, `{model}`, `{agent}`, and `{mode}`. Each array item becomes one command-line argument, so spaces in a task stay within its argument. To disable a built-in agent without removing its settings, set `"enabled": false`.

For a custom agent named `my-agent`, set `AGENT_ORCHESTRATOR_BIN_MY_AGENT` in the MCP server environment to override its executable path. Built-in path overrides are `AGY_BIN`, `CLAUDE_BIN`, and `OPENCODE_BIN`.

After changing the file, restart the host application so it reloads the agent list and profile choices in the MCP tool schemas.
