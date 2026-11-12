# Configuration

The bridge reads `~/.config/agent-orchestrator/config.json` at startup. Set `XDG_CONFIG_HOME` to use a different configuration directory. The file is shared by all orchestrator hosts on this machine.

## Model preferences

Give an agent a normal model with `preferred_model`. Define named profiles for exceptions, then pass the profile name to `delegate_agent` or `consult_agents` when needed:

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
    "claude": {"preferred_model": "sonnet"},
    "opencode": {"preferred_model": "provider/model-id"}
  }
}
```

Replace the example identifiers with models available in the corresponding CLI. A model in the selected profile takes precedence over `preferred_model`; if neither is set, the bridge leaves model selection to the CLI. Antigravity and Claude Code accept their own model identifier with `--model`; OpenCode uses its provider-qualified model identifier. See the [Antigravity CLI guide](https://antigravity.google/docs/cli/headless/), [Claude Code CLI reference](https://docs.anthropic.com/en/docs/claude-code/cli-usage), and [OpenCode model guide](https://opencode.ai/docs/models/).

The bridge includes a `native` profile when no configuration file exists. If the file exists, its `default_profile` must name one of the configured profiles. An omitted `profile` uses this default.

## Add a CLI agent

Add an entry under `agents`. The bridge builds a subprocess argument list directly; it does not run the configured command through a shell.

```json
{
  "agents": {
    "my-agent": {
      "command": "my-agent",
      "label": "My coding agent",
      "preferred_model": "model-name",
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
