# Codex agent orchestrator

Route bounded tasks from Codex CLI to Antigravity, Claude Code, and OpenCode, ask independent agents for parallel advice, and review implementation changes in a separate Git worktree.

[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

---

## Quick start

Install Python 3.9 or newer, [Codex CLI](https://github.com/openai/codex), and at least one supported coding agent. From the repository directory, add the bridge to Codex:

```bash
codex mcp add local-agents -- python3 "$PWD/src/codex_agent_bridge.py"
```

Then add these settings to `~/.codex/config.toml` under `[mcp_servers.local-agents]`:

```toml
tool_timeout_sec = 1000
default_tools_approval_mode = "prompt"
```

Restart Codex CLI. Ask Codex to delegate a bounded task or request a second opinion. For implementation, the bridge creates a new branch and worktree and returns a diff; Codex reviews and integrates it.

## What it does

- **`agent_catalog`** lists supported command-line agents and routing guidance.
- **`consult_agents`** asks two or three different agents for independent opinions at the same time, in their read-only planning modes.
- **`delegate_agent`** runs one agent in research mode or implementation mode. Implementation starts from the repository's current `HEAD` in a new Git worktree.
- **Review before integration.** The bridge returns the worktree path, branch, changed files, summary, diff, and the agent's response. It does not merge or push.
- **Use existing logins.** The bridge starts the installed CLIs; it does not request, read, or store provider credentials itself.

The worktree separates normal file edits from the main checkout. It is not an operating-system sandbox: each CLI retains the permissions of the current user. OpenCode's implementation command uses its automatic permission mode. Only delegate work in repositories and with prompts you trust.

## Screenshots

This project is a terminal service with no graphical interface. Its use is shown with the command examples above.

## Configuration

Codex stores the MCP connection in `~/.codex/config.toml`. The command above records an absolute script path. Set the per-server timeout and approval behavior:

| Setting | Value | What it does |
| --- | --- | --- |
| `tool_timeout_sec` | `1000` | Leaves room for the bridge's 900-second implementation limit. |
| `default_tools_approval_mode` | `"prompt"` | Asks before a model invokes a bridge tool. |

The bridge searches `PATH` for `agy`, `claude`, and `opencode`. Override a path when needed with `AGY_BIN`, `CLAUDE_BIN`, or `OPENCODE_BIN` in the MCP server environment. The variable must point to an installed executable.

| Bridge limit | Default | What it limits |
| --- | --- | --- |
| Task length | 20,000 characters | A single delegated prompt. |
| Consultation timeout | 360 seconds | Each read-only agent call. |
| Implementation timeout | 900 seconds | One implementation call. |
| Returned agent output | 18,000 characters | The tail of each CLI response. |
| Consultation agents | 3 | Maximum parallel opinions. |

## How it works

Codex calls the bridge over MCP (Model Context Protocol) using standard input and output. The bridge starts each provider's own CLI, so authentication and model selection stay in that CLI. Consultation runs up to three child processes concurrently. Implementation records the base commit, creates a unique branch and worktree, runs one agent there, and returns the resulting state for Codex to review. See [the architecture guide](docs/architecture.md) for the flow and limits.

## Development

The bridge uses only the Python standard library. Run its checks with:

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q src
```

## License

[MIT](LICENSE), © Alexander Blinov.
