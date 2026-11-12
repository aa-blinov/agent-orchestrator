# Architecture

The bridge is a single Python 3 process using only the standard library. Codex communicates with it over JSON-RPC on stdin and stdout. The bridge launches each provider's native command-line client rather than proxying its model API.

## The shape

| Module | Responsibility |
| --- | --- |
| `src/codex_agent_bridge.py` | MCP tool catalog, argument validation, child process lifecycle, Git worktree preparation, and result formatting. |
| `tests/test_bridge.py` | Focused checks for protocol handling and CLI argument construction. |

## The data flow

```text
Codex CLI
  → JSON-RPC request
  → bridge validates agent, task, mode, and project path
  → native agent CLI
  → text and Git status/diff
  → JSON-RPC tool result
  → Codex reviews and integrates
```

Consultation starts up to three planning calls in a thread pool, each as a separate process group. Implementation records the source repository's `HEAD`, creates a unique branch and worktree under `~/.codex/agent-worktrees/`, then runs one agent in that worktree. New files are included by `git add -N` before the bridge collects the diff.

## Decisions worth knowing about

### Keep provider authentication native

Each agent CLI reads its own login and model configuration. The bridge only starts the executable and does not read or copy authentication files. The cost is that each CLI must already be installed and logged in.

### Use synchronous tools

The bridge keeps the interaction simple: Codex waits for a result and receives the diff in the same tool call. Codex's MCP timeout is configured to 1,000 seconds to exceed the 900-second implementation limit. The cost is that one long-running MCP call blocks the bridge's request loop.

### A worktree is not a sandbox

The separate branch prevents ordinary edits from mixing with the main checkout. It does not restrict access to other files or Git metadata. OpenCode implementation uses automatic approval to run unattended. Do not treat this as a security boundary or delegate untrusted tasks.

### Start from committed state

Worktrees are created from the source repository's `HEAD`. Uncommitted changes are deliberately not copied; the bridge reports that fact. This avoids copying ignored secrets or temporary artifacts into a delegated checkout.

## Boundaries

- The bridge must not merge, push, or delete a user's branch.
- Provider credentials remain in their provider's CLI.
- Codex is the only integration owner; it reviews the returned diff and runs project checks.
- The bridge has no hosted endpoint, telemetry, API key store, or network client of its own.
