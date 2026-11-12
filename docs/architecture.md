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

The default routing policy is one lead and one implementer. The lead inspects the result and runs relevant checks; for high-risk changes it can ask one other agent for a focused read-only review. For a material unresolved question, it can ask one agent for research. Parallel consultation is reserved for cases where comparing two or three independent opinions could change a high-stakes decision, and replaces a single research call. Retry only after a failed call or a concrete finding. The bridge enforces at most four agent CLI launches and three distinct peer agents per caller-supplied `session_id`, shared by both delegation tools. `task_id` independently groups usage for a task. MCP clients do not give this local stdio server a standard chat ID, so the host creates one `session_id` per chat and reuses it; the limit is hard for that ID, not an authenticated host identity.

Consultation starts up to three planning calls in a thread pool, each as a separate process group. Implementation records the source repository's `HEAD`, creates a unique branch and worktree under `~/.agent-orchestrator/worktrees/`, then runs one agent in that worktree. New files are included by `git add -N` before the bridge collects the diff.

Each CLI runs in its machine-readable output mode where available. The bridge extracts its reported token usage and keeps the final answer separate from the usage ledger. The ledger is a local SQLite database under the user's data directory; it stores task IDs, agent/model/mode, exit status, duration, and counters, but no prompts or responses. Absent usage fields stay unavailable. Provider counters can differ in how they account for cache and reasoning tokens, so reports sum counters only within one agent and requested model, never across agents, and the sums are informational rather than converted to cost.

## Decisions worth knowing about

### Keep provider authentication native

Each agent CLI reads its own login configuration. The bridge only starts the executable and does not read or copy authentication files. An optional model ID can be supplied from the shared model profile; otherwise the CLI's own model choice applies. The cost is that each CLI must already be installed and logged in.

### Use synchronous tools

The bridge keeps the interaction simple: the selected host waits for a result and receives the diff in the same tool call. Both research and implementation calls can run for up to 7200 seconds, so configure the host MCP timeout to 7200 seconds or more. The cost is that one long-running MCP call blocks the bridge's request loop.

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
