# Changelog

All notable changes to Agent Orchestrator. Versions are ordered newest first.

## [Unreleased]

## [0.4.3] - 2026-11-12

### Fixed

- Detect structured Antigravity and Codex failures even when their CLI exits with status zero.
- Return tool validation and execution errors using MCP's `isError` result field.
- Run MCP tool calls concurrently so the server can receive cancellation notifications during long agent calls.
- Stop cancelled child process groups and suppress responses for cancelled requests.

## [0.4.2] - 2026-11-12

### Added

- Added an enforced per-chat budget of four agent CLI calls and three distinct peer agents, keyed by a stable `session_id`.
- Added a local Codex plugin package with bundled MCP configuration and an agent-routing skill.
- Added server-level routing instructions for cost-aware delegation.
- Added selectable MCP hosts through `AGENT_ORCHESTRATOR_HOST`.
- Excluded the active host from peer tools to prevent recursive delegation.
- Renamed the project and bridge to host-neutral names.
- Added a shared JSON configuration for built-in and custom CLIs, preferred models, and per-task model profiles.
- Added separate preferred models for research and implementation roles.
- Published the Codex plugin package with a reusable routing skill.
- Set the maximum consultation and implementation call duration to two hours.

## 0.1.0 (2026)

### Added

- Added a local MCP bridge for consultation and delegated implementation with Antigravity, Claude Code, and OpenCode.
- Added separate Git worktrees for implementation results and reviewable diffs.
