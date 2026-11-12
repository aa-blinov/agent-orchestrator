# Architecture

The bridge is a single Python 3 process using only the standard library. The selected host communicates with it over JSON-RPC on stdin and stdout. The bridge launches each provider's native command-line client rather than proxying its model API.

## The shape

| Module | Responsibility |
| --- | --- |
| `src/agent_orchestrator.py` | MCP tool catalog, argument validation, child process lifecycle, Git worktree preparation, and result formatting. |
| `tests/test_agent_orchestrator.py` | Focused checks for protocol handling and CLI argument construction. |

## The data flow

```text
Selected host CLI
  → JSON-RPC request
  → bridge validates agent, task, mode, and project path
  → native agent CLI
  → text and Git status/diff
  → JSON-RPC tool result
  → The selected host reviews and integrates
```

Consultation starts up to three planning calls in a thread pool, each as a separate process group. Implementation records the source repository's `HEAD`, creates a unique branch and worktree under `~/.agent-orchestrator/worktrees/`, then runs one agent in that worktree. New files are included by `git add -N` before the bridge collects the diff.

## Decisions worth knowing about

### Keep provider authentication native

Each agent CLI reads its own login configuration. The bridge only starts the executable and does not read or copy authentication files. An optional model ID can be supplied from the shared model profile; otherwise the CLI's own model choice applies. The cost is that each CLI must already be installed and logged in.

### Use synchronous tools

The bridge keeps the interaction simple: the selected host waits for a result and receives the diff in the same tool call. Configure its MCP timeout above 900 seconds to exceed the implementation limit. The cost is that one long-running MCP call blocks the bridge's request loop.

### A worktree is not a sandbox

The separate branch prevents ordinary edits from mixing with the main checkout. It does not restrict access to other files or Git metadata. OpenCode implementation uses automatic approval to run unattended. Do not treat this as a security boundary or delegate untrusted tasks.

### Start from committed state

Worktrees are created from the source repository's `HEAD`. Uncommitted changes are deliberately not copied; the bridge reports that fact. This avoids copying ignored secrets or temporary artifacts into a delegated checkout.

## Boundaries

- The bridge must not merge, push, or delete a user's branch.
- Provider credentials remain in their provider's CLI.
- Agent commands and model preferences can be extended in `~/.config/agent-orchestrator/config.json` without editing the bridge.
- The selected host is the integration owner; it reviews the returned diff and runs project checks.
- The bridge has no hosted endpoint, telemetry, API key store, or network client of its own.
