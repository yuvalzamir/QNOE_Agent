# HOME — Claude Code Memory Index
*Last updated: 2026-07-14 (B7 systemd sandbox)*

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
| [[CONTEXT_PRESSURE_REPORT]] | Context-pressure analysis + roadmap (KV math, demand reduction, MoE model swap, 2-Spark scale-out) — user-accepted 2026-07-09 |
| [[CONTEXT_EXECUTION_PLAN]] | Hand-off plan for roadmap steps 1-3 (vLLM 64K+fp8, toolset slimming, Provence) — executed 2026-07-09/10 |
| [[GPT_OSS_CUTOVER_PLAN]] | Hand-off plan for the production cutover to gpt-oss-120b via llama.cpp — executed 2026-07-10 |
| [[GPT_OSS_PILOT_PLAN]] | Pilot plan for gpt-oss-120b — vLLM path FAILED (M39/M41); llama.cpp path won → see [[GPT_OSS_CUTOVER_PLAN]] |
| [[USER_GUIDE]] | Plain-language guide for lab members (find files, look up measurement runs) — WIP |
| [[MVP_VERIFICATION_PLAN]] | The 4 remaining MVP-1 acceptance tests (routing, RAG-paper, /switch recast, /help recast) — pre-declaration checklist |
| `redteam/BACKLOG.md` | Red-team harness findings log (R1-R4, M47) — the adversarial test loop's memory |
| [[MEM0_INTEGRATION]] | Program to add per-user Mem0 memory inside `qnoe_rag` (see [[memory/decisions#D13]]) |
| [[REPO_MAPPING]] | Repo → Qdrant collection mapping |
| [[WATCHER_PLAN]] | SMB3 watcher daemon design |

## Active Workstream

**MVP-1 DECLARED (2026-07-10)** — all rescoped acceptance criteria pass (see [[SETUP_LOG]] declaration section). Stack: gpt-oss-120b via llama.cpp (4×64K), 3 Hermes profiles, hybrid RAG + Mem0 + QCoDeS registry grounding, T0/T1 read-only.

**Red-team loop (2026-07-14)** — built a repeatable adversarial harness (`redteam/`, findings in `redteam/BACKLOG.md`). Rounds 1-2 found+fixed: R1/R2 tool-selection (Tool Search now OFF → qcodes tools resident; `tool_use_enforcement` back true), **M47 mass Mem0 poisoning** (purged 40+ pre-fix confabulated facts), R3 calibration, R4 read-only. R2 residual (~60% "latest sweep" reliability, gpt-oss intermittency) DEFERRED. Injection/refusal/attribution/run-existence confirmed solid.
Next: re-verify R3/R4, add fresh probe classes; Phase 2 ([[PHASE2_BACKLOG]] B8/B9/B10); PPTX Gantt before PI presentation.

**B7-OS: OpenShell sandbox IS PRODUCTION (2026-07-14 evening)** — same-day supersession of the systemd drop-in ([[memory/decisions#D18]] over D17): user rejected systemd's cons, OpenShell v0.0.82 fixed the bind-mount blocker, full migration executed + verified in one session (probe 24/24 in-sandbox; Teams/Mem0/RAG/qcodes checks passed; R4 perm-write physically EROFS-blocked with model attempting; failure drills passed). `qnoe-hermes-sandbox.service` enabled at boot; old unit = one-command rollback. Traps + lessons: `memory/mistakes.md` M50 (live↔repo drift clobbered hotfixes) + M51 (five OpenShell runtime traps). Soak pending: nightly cron visibility next morning, SharePoint query. Details: [[memory/infrastructure]] §B7-OS.
