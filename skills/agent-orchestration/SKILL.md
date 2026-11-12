---
name: agent-orchestration
description: Route difficult coding work to installed local CLI agents with a small, reviewable call budget. Use when the active coding assistant needs implementation help, a second opinion, or an independent code review through the local-agents MCP server.
---

# Local agent routing

Use the smallest team that can complete the task reliably.

1. The active coding assistant is the lead. Usually delegate to one implementer, then inspect the returned diff and run the relevant project checks yourself.
2. Ask one agent for read-only research or review only when a concrete uncertainty or the cost of a defect justifies the extra call.
3. Use `consult_agents` with two or three agents only when comparing independent opinions could materially change a high-stakes decision. It consumes one call per agent and replaces a single research call; do not also ask the same question through `delegate_agent`.
4. Retry only after a failed call or a concrete finding that needs correction. Reuse an existing agent when practical.

## Chat call limit

At the start of a chat, create one stable `session_id` such as `chat-<random-uuid>`. Keep using that exact value in every `delegate_agent` and `consult_agents` call for the rest of the chat, including across separate tasks. Start a new ID only in a genuinely new chat. This ID is supplied by the caller because most MCP clients do not provide the host chat ID to this local server.

The bridge enforces a combined maximum of four agent CLI launches and three distinct peer agents for one `session_id`. A parallel consultation with two agents consumes two launches. The bridge rejects an over-budget request before starting any agent. Use a separate `task_id` for each logical task and reuse it when inspecting that task's `usage_report`.

The quota is keyed to the supplied `session_id`; never change it to get more calls. MCP calls that do not launch agents, such as `usage_report`, do not consume this quota. The server cannot independently authenticate that the ID is the host's actual chat ID.

## Delegation details

- Pass the repository directory as `cwd`.
- For implementation, give one bounded deliverable and explicit acceptance checks.
- The implementation agent works in a separate Git worktree. Review its branch, status, and diff before integration.
- Do not ask the bridge to repeat an already successful review without a new finding.
