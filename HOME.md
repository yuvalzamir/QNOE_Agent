# HOME — Claude Code Memory Index
*Last updated: 2026-07-08*

> **Purpose:** Persistent memory for Claude Code working on the QNOE Lab Agent project.
> Start every session here. Follow links to topic files as needed.

## Quick Start

1. Read this file
2. Check [[SETUP_LOG]] for latest DGX state
3. Check [[TODO]] for current priorities

## Session Rules

- Ask before SSH at session start; once approved, use freely
- Never restart vLLM unless absolutely necessary (5+ min reload)
- Deploy via `/tmp/` → `sudo cp` → `sudo chown qnoe-ai:qnoe-ai` → `sudo chmod g+w`
- Update [[SETUP_LOG]] as steps complete
- Read files before editing — never overwrite user's work

## Memory Topics

| File | What's in it |
|---|---|
| [[memory/infrastructure]] | DGX, vLLM, Qdrant, Docker, systemd, CIFS, cron |
| [[memory/agent-code]] | Agent files, message flow, tools, LLM client |
| [[memory/ingestion]] | RAG pipeline, scanners, watcher daemon, nightly jobs |
| [[memory/decisions]] | Architectural decisions log |
| [[memory/mistakes]] | Bugs fixed, pitfalls, hard-won technical fixes |
| [[memory/hermes-migration]] | Migration from LangGraph to Hermes Agent |
| [[memory/deploy-patterns]] | DGX file ownership, deployment procedures |
| [[memory/user-preferences]] | How the user works, communication style |

## Project Files (not memory — reference docs)

| File | Role |
|---|---|
| [[HANDOFF]] | Single-page summary of all decisions |
| [[SETUP_LOG]] | Current DGX state — check each session |
| [[TODO]] | Master task list |
| [[AGENT_CODE_GUIDE]] | Message flow, file roles, routing |
| [[DGX_SETUP]] | Step-by-step DGX setup |
| [[AGENT_FRAMEWORK]] | LangGraph design, system prompts, MVP scope |
| [[MIGRATION_PLAN]] | Hermes Agent migration phases M1–M8 |
| [[PHASE2_BACKLOG]] | Post-MVP features B1–B7 |
| [[MEM0_INTEGRATION]] | Program to add per-user Mem0 memory inside `qnoe_rag` (see [[memory/decisions#D13]]) |
| [[REPO_MAPPING]] | Repo → Qdrant collection mapping |
| [[WATCHER_PLAN]] | SMB3 watcher daemon design |

## Active Workstream

**Hermes Agent Migration** — M1–M7.5 DONE (full migration + per-user profile routing). Remaining: M8 cleanup, tool calling fix. See [[memory/hermes-migration]].
