# Agent orchestrator

Use Codex, Antigravity, Claude Code, or OpenCode as the orchestrator. The active orchestrator can route bounded tasks to its peers, ask for parallel advice, and review implementation changes in a separate Git worktree.

[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

---

## Quick start

Install Python 3.9 or newer and the coding CLIs you want to use. Register the same bridge with each orchestrator you want to switch between. Run these commands from this repository directory:

```bash
codex mcp add --env AGENT_ORCHESTRATOR_HOST=codex local-agents -- python3 "$PWD/src/agent_orchestrator.py"
agy mcp add --env AGENT_ORCHESTRATOR_HOST=agy local-agents python3 "$PWD/src/agent_orchestrator.py"
claude mcp add --scope user --env AGENT_ORCHESTRATOR_HOST=claude local-agents -- python3 "$PWD/src/agent_orchestrator.py"
opencode mcp add local-agents --global --env AGENT_ORCHESTRATOR_HOST=opencode -- python3 "$PWD/src/agent_orchestrator.py"
```

For Codex, timeout and approval settings go in `~/.codex/config.toml` under `[mcp_servers.local-agents]`:

```toml
tool_timeout_sec = 1000
default_tools_approval_mode = "prompt"
```

Restart the CLIs after registering. Whichever one you launch is the lead for that session. It can delegate bounded work or ask peers for a second opinion. The bridge removes the active lead from its own peer list to prevent recursive calls. For implementation, it creates a new branch and worktree and returns a diff; the lead reviews and integrates it.

## What it does

- **`agent_catalog`** lists available command-line agents, preferred models, and model profiles.
- **`consult_agents`** asks two or three different agents for independent opinions at the same time, in their read-only planning modes; optionally select a model profile.
- **`delegate_agent`** runs one agent in research mode or implementation mode. Implementation starts from the repository's current `HEAD` in a new Git worktree; optionally select a model profile.
- **Token usage** is recorded locally after each agent call, separately from its response. Use `usage_report` to inspect recent tasks or calls for a `task_id`. Pass the same `task_id` to group several agent calls into one task. Missing counts are marked as unavailable; prompts are not stored and the bridge does not estimate counts or convert them to money.
- **Review before integration.** The bridge returns the worktree path, branch, changed files, summary, diff, and the agent's response. It does not merge or push.
- **Use existing logins.** The bridge starts the installed CLIs; it does not request, read, or store provider credentials itself.

The worktree separates normal file edits from the main checkout. It is not an operating-system sandbox: each CLI retains the permissions of the current user. OpenCode's implementation command uses its automatic permission mode. Only delegate work in repositories and with prompts you trust.

For a concrete complex-task example, including an end-to-end DAG and sample MCP calls, see [the Russian workflow guide](docs/example-complex-task.ru.md). It shows how independent read-only analysis, isolated implementation, and review are intended to reduce reliance on one agent's first framing without promising to eliminate model bias.

## Screenshots

This project is a terminal service with no graphical interface. Its use is shown with the command examples above.

## Configuration

Each CLI stores its own MCP connection. The commands above record an absolute script path and select the active lead with `AGENT_ORCHESTRATOR_HOST`. Agent and model preferences are shared in `~/.config/agent-orchestrator/config.json`; the bridge reads this file when each CLI starts.

Give each agent separate `research` (complex analysis and consultation) and `implement` (making changes) models. Named profiles can override them for a particular task. If a model is missing, the CLI uses its own configured default. For example:

```json
{
  "default_profile": "balanced",
  "profiles": {
    "balanced": {"description": "Normal choices", "models": {}},
    "strong": {
      "description": "Use the stronger model for difficult tasks",
      "models": {
        "claude": "sonnet",
        "opencode": "provider/model-id"
      }
    }
  },
  "agents": {
    "claude": {
      "preferred_models": {
        "research": "opus",
        "implement": "sonnet"
      }
    }
  }
}
```

The leading agent can pass `profile: "strong"` to `delegate_agent` or `consult_agents`. Consultation and research use the `research` model; implementation uses the `implement` model. To add a CLI, add an entry under `agents` with its executable, label, mode arguments and task arguments; the example in [the configuration guide](docs/configuration.md) shows the complete entry. Commands are passed as argument arrays without a shell. Restart the leading CLI after editing the file.

Codex-specific timeout and approval settings go in `~/.codex/config.toml`:

| Setting | Value | What it does |
| --- | --- | --- |
| `tool_timeout_sec` | `1000` | Leaves room for the bridge's 900-second implementation limit. |
| `default_tools_approval_mode` | `"prompt"` | Asks before a model invokes a bridge tool. |

The bridge searches `PATH` for configured executables. Override built-in paths with `AGY_BIN`, `CLAUDE_BIN`, or `OPENCODE_BIN`; custom agent `my-agent` can use `AGENT_ORCHESTRATOR_BIN_MY_AGENT`. The variable must point to an installed executable. Authentication remains in each CLI; the bridge passes a model only when one is set in the selected profile or as the agent's preference for that role.

| Bridge limit | Default | What it limits |
| --- | --- | --- |
| Task length | 20,000 characters | A single delegated prompt. |
| Consultation timeout | 360 seconds | Each read-only agent call. |
| Implementation timeout | 900 seconds | One implementation call. |
| Returned agent output | 18,000 characters | The tail of each CLI response. |
| Consultation agents | 3 | Maximum parallel opinions. |

## How it works

The active host calls the bridge over MCP (Model Context Protocol) using standard input and output. The bridge starts each provider's own CLI, so authentication stays in that CLI. Consultation runs up to three child processes concurrently. Implementation records the base commit, creates a unique branch and worktree, runs one peer there, and returns the resulting state for the lead to review. See [the architecture guide](docs/architecture.md) for the flow and limits.

## Development

The bridge uses only the Python standard library. Run its checks with:

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q src
```

## License

[MIT](LICENSE), © Alexander Blinov.
