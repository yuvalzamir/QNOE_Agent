# HOME — Claude Code Memory Index
*Last updated: 2026-07-20 (Cognee pilot state reconciled; M60 load-average lesson)*

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
| [[CONTEXT_BLOCK_TRACKING_PLAN]] | Context-block tracking (threat-scanner drop tally + hourly scan + nightly report line) — executed 2026-07-16 |
| [[USER_GUIDE]] | Plain-language guide for lab members (find files, look up measurement runs) — WIP |
| [[MVP_VERIFICATION_PLAN]] | The 4 remaining MVP-1 acceptance tests (routing, RAG-paper, /switch recast, /help recast) — pre-declaration checklist |
| `redteam/BACKLOG.md` | Red-team harness findings log (R1-R4, M47) — the adversarial test loop's memory |
| [[MEM0_INTEGRATION]] | Program to add per-user Mem0 memory inside `qnoe_rag` (see [[memory/decisions#D13]]) |
| [[MEMORY_ARCHITECTURE]] | Two-layer memory design: Cognee corpus-KG + Mem0 personalization; first-party/third-party boundary; decisions ([[memory/decisions#D20]]) |
| [[MEM0_HYGIENE_OPTIONS]] | Options menu for Mem0 provenance/audit/extraction-hygiene |
| [[COGNEE_PLAN]] | Cognee corpus knowledge-graph (L5) — framework choice, config, dev/deploy phases |
| [[COGNEE_ONTOLOGY]] | The KG schema — two tiers, node/edge types (research program, grounded in QNOE physics) |
| [[KG_ONEPAGER]] | One-page KG summary for the PI (Frank) |
| [[REPO_MAPPING]] | Repo → Qdrant collection mapping |
| [[WATCHER_PLAN]] | SMB3 watcher daemon design |

## Active Workstream

**SharePoint coverage audit → re-ingest → sweep — CLOSED OUT (2026-07-16→18)** — SP had its own M58-class silent gap: `sharepoint_coverage_audit.py` found noe-group `General` at 53%; forensics (Jul-8 listing cache) split the 11.7K missing into no-text plot/figure PDFs vs 2,443 real files (theses, Fastera manuscripts); targeted semaphore re-ingest recovered 1,218 (Patents 92%, NOE-FAB 87%); "8,320 orphans" premise REVERSED (0 truly gone — pre-exclusion `.ipynb_checkpoints`/`.txt` rows) → sweep purged 7,719 rows + 230K Qdrant chunks; `task_sp_coverage` now a nightly standing check. OCR on 957 scanned PDFs dropped (user). Open user decisions (plot-class policy, `.txt` purge, 2 unconfigured sites) in [[TODO]]; full record [[memory/ingestion]] §SharePoint coverage audit.

**Memory architecture — Cognee corpus-KG + Mem0 thinned (design, 2026-07-17)** — decided the future memory system ([[memory/decisions#D20]], [[MEMORY_ARCHITECTURE]]): a **Cognee** knowledge-graph of the group's *research program* (L5; concepts/questions/techniques/setups/projects anchored to measurement data) + **Mem0** narrowed to per-user personalization. Two tiers of truth — Tier 1 factual anchor (registry via `add_data_points`, deterministic) + Tier 2 research/conceptual (`cognify`, LLM-inferred, provenance-tagged). Framework = Cognee (beat LightRAG/GraphRAG/Graphiti — [[COGNEE_PLAN]]). Ontology in [[COGNEE_ONTOLOGY]]; PI one-pager [[KG_ONEPAGER]]. **Interim Mem0 de-risk shipped:** provenance metadata on writes (commit 00d2ba8) + a first-party/third-party write-gate classifier (`memory_gate.py`, 29/29 offline). **Pilot COMPLETE (2026-07-21 13:44, rc=0, 31/31 batches, medium effort):** the QTM knowledge graph is BUILT — **14,517 entities / 11,943 relations, all 17 ontology types populated** (untyped 31% = the tuning dial). Exports in-repo: `cognee/output/qtm_full.{md,json}`. Getting there took five configurations across 2026-07-18→21; the four root causes and their fixes (thread caps/URL-strip, litellm 600s timeout, per-batch datasets — cognify processes ALL pending docs in a dataset, chunk_size 2048 vs the ~40GB ONNX embedding balloon, plus cognee-native data_per_batch/chunks_per_batch caps) are recorded in [[TODO]] §Cognee and the winning config is `cognee/run_pilot.py`. **OPEN GATE: human-judge `qtm_full.md` vs [[COGNEE_ONTOLOGY]] §4** — go/no-go for Tier-2; if go → Phase 1 registry backbone (`add_data_points`).

**Context-block tally LIVE (2026-07-16)** — no threat-scanner drop is silent anymore: hourly `qnoe-context-tally.timer` (qnoe-ai, outside sandbox) parses profile agent.logs (two warning formats) → `logs/context_blocks.jsonl` + hourly `soul_health.py` rescan (rewritten — now mirrors BOTH scan surfaces: SOUL context-scope + `memories/` per-entry strict-scope); nightly `task_context_blocks` + Teams-report line with staleness/format-drift self-monitoring. Plan+deltas: [[CONTEXT_BLOCK_TRACKING_PLAN]]; details [[memory/agent-code#Context-block tally]]; decision [[memory/decisions#D19]]. Ride-along: first real `memory_entry` event.

**MVP-1 DECLARED (2026-07-10)** — all rescoped acceptance criteria pass (see [[SETUP_LOG]] declaration section). Stack: gpt-oss-120b via llama.cpp (4×64K), 3 Hermes profiles, hybrid RAG + Mem0 + QCoDeS registry grounding, T0/T1 read-only.

**Red-team loop (2026-07-14)** — built a repeatable adversarial harness (`redteam/`, findings in `redteam/BACKLOG.md`). Rounds 1-2 found+fixed: R1/R2 tool-selection (Tool Search now OFF → qcodes tools resident; `tool_use_enforcement` back true), **M47 mass Mem0 poisoning** (purged 40+ pre-fix confabulated facts), R3 calibration, R4 read-only. R2 residual (~60% "latest sweep" reliability, gpt-oss intermittency) DEFERRED. Injection/refusal/attribution/run-existence confirmed solid.
Next: re-verify R3/R4, add fresh probe classes; Phase 2 ([[PHASE2_BACKLOG]] B8/B9/B10); PPTX Gantt before PI presentation.

**B7-OS: OpenShell sandbox IS PRODUCTION (2026-07-14 evening)** — same-day supersession of the systemd drop-in ([[memory/decisions#D18]] over D17): user rejected systemd's cons, OpenShell v0.0.82 fixed the bind-mount blocker, full migration executed + verified in one session (probe 24/24 in-sandbox; Teams/Mem0/RAG/qcodes checks passed; R4 perm-write physically EROFS-blocked with model attempting; failure drills passed). `qnoe-hermes-sandbox.service` enabled at boot; old unit = one-command rollback. Traps + lessons: `memory/mistakes.md` M50 (live↔repo drift clobbered hotfixes) + M51 (five OpenShell runtime traps). Soak pending: nightly cron visibility next morning, SharePoint query. Details: [[memory/infrastructure]] §B7-OS.
