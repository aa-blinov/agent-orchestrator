# Changelog

All notable changes to Agent Orchestrator. Versions are ordered newest first.

## [Unreleased]

### Added

- Added selectable MCP hosts through `AGENT_ORCHESTRATOR_HOST`.
- Excluded the active host from peer tools to prevent recursive delegation.
- Renamed the project and bridge to host-neutral names.
- Added a shared JSON configuration for built-in and custom CLIs, preferred models, and per-task model profiles.
- Added separate preferred models for research and implementation roles.

## 0.1.0 (2026)

### Added

- Added a local MCP bridge for consultation and delegated implementation with Antigravity, Claude Code, and OpenCode.
- Added separate Git worktrees for implementation results and reviewable diffs.
