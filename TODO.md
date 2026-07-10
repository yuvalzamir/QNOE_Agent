# QNOE Lab Agent — Master TODO
*Last updated: 2026-07-09 — BM25 backfill complete; repo change queue processed (parallel 6-worker runner); nightly cron re-enabled; SP nightly task re-enabled; vLLM restarted; watcher path exclusions added (PyInstaller/venv/ipynb_checkpoints); Mem0 deploy pending*

> Claude Code memory: [[HOME]] · Migration tracker: [[memory/hermes-migration]] · Decisions: [[memory/decisions]]

---

## ⏳ PENDING DEPLOY — Mem0 per-user memory

**Status:** Built + validated on branch `feature/mem0-per-user` (4 commits). **NOT deployed.** Waiting on a vLLM window (vLLM is down for the SharePoint full sync; starting it now risks OOMing the sync). Full design: [[MEM0_INTEGRATION]] · decision [[memory/decisions#D13]].

**What it does:** per-user long-term memory via the Mem0 library inside `qnoe_rag` (RAG stays the single injector). Drops `USER.md`, keeps `MEMORY.md`.

**Already done (safe, additive):** `mem0ai` 2.0.11 installed in `hermes-venv`; Qdrant `episodic_memory` collection + `user_id` index created; validated offline (schema, embedder, write/read, per-user isolation).

**How to deploy** — after the SP sync finishes, when vLLM can start safely:

1. **Check artifacts are staged on the DGX** (re-`scp` if `/tmp` was cleared):
   ```bash
   ls -la /tmp/qnoe_rag_init.py /tmp/deploy_mem0.sh
   # if missing, from the workstation:
   #   scp hermes/plugins/qnoe_rag/__init__.py yzamir@10.3.8.21:/tmp/qnoe_rag_init.py
   #   scp scripts/deploy_mem0.sh              yzamir@10.3.8.21:/tmp/deploy_mem0.sh
   #   ssh … "sed -i 's/\r$//' /tmp/deploy_mem0.sh /tmp/qnoe_rag_init.py"
   ```
2. **Run the staged deploy** (deploys plugin + sets `user_profile_enabled: false` in the 3 profile configs; reversible, no restart):
   ```bash
   bash /tmp/deploy_mem0.sh
   ```
3. **Confirm the vLLM model id** for Mem0's fact-extraction LLM (open item):
   ```bash
   curl -s http://localhost:8000/v1/models     # if not "hermes-3-70b", set MEM0_LLM_MODEL
   ```
4. **Start vLLM, then restart the agent:**
   ```bash
   sudo systemctl start vllm.service           # ~5 min to load
   sudo systemctl restart qnoe-hermes.service
   ```
5. **Verify** (details in [[MEM0_INTEGRATION]] §9): logs show `Initializing Mem0`; a stated preference lands in `episodic_memory`; next turn shows `## What I remember about you`; **user B does not see user A's fact**; tool calls still fire (watch the ~19.5K context cliff); watch for protobuf errors (mem0 downgraded protobuf 7→6).

**Rollback:** `export MEM0_ENABLED=0` + restart (fast); or remove the `user_profile_enabled: false` line + `git checkout master -- hermes/plugins/qnoe_rag/__init__.py` + redeploy.

**Still untested (needs vLLM):** `add(infer=True)` LLM distillation; live in-agent behavior.

- [ ] **Deploy Mem0 per-user memory** — run `deploy_mem0.sh` + steps above once vLLM window is available

---

## ⏳ ACCEPTED — Context-pressure package (2026-07-09)

**Status:** Roadmap steps 1-5 of [[CONTEXT_PRESSURE_REPORT]] accepted by user (see inline **→ Answer/Decision** blocks). Requirement: **≥3 concurrent users**. §2.1 open check RESOLVED 2026-07-09 (core tools never defer — slimming via toolset composition). **Steps 1-3 handed off to an executing agent: [[CONTEXT_EXECUTION_PLAN]].** vLLM is currently STOPPED; BM25 backfill COMPLETE (2026-07-09 ~12:04 UTC) — no blocker for the restart.

- [ ] **1. vLLM flags** (per [[CONTEXT_EXECUTION_PLAN]] §1): `--max-model-len 65536 --kv-cache-dtype fp8 --max-num-seqs 4` in `scripts/start_vllm.sh`; benchmark fp8 vs fp16, keep winner; `context_length: 65536` in all 3 Hermes profiles.
- [ ] **2. Tool-schema slimming** (per plan §2): `toolsets: [file, terminal, clarify, qnoe-lab]` replacing `hermes-cli` in all 3 profiles (~6.4K → ~3.7K tok). NOT via Tool Search — core tools never defer (verified in v0.17.0 source; Tool Search is currently a no-op for our profiles).
- [ ] **3. Provence reranker swap** (per plan §3): download `naver/provence-reranker-debertav3-v1`, 20-query eval gate vs cross-encoder, integrate behind `RAG_RERANKER` env flag.
- [x] **4. Prefix caching: VERIFIED (2026-07-10).** `enable_prefix_caching=True` (V1 startup log); live metrics show **81.5% prefix cache hit rate**; injection point confirmed in Hermes source (`conversation_loop.py`): RAG/Mem0 block is appended to the current turn's *user message* (end of prompt), so the SOUL+tools+history prefix stays cacheable. No change needed.
- [x] **5. ~19.5K tool-calling cliff: RESOLVED (2026-07-10) — it does not exist as a model limit.** Bare probes held structured tool calls to 32.4K; live failure is prose-fallback driven by context *composition* (tool-catalog size, tool-output length, chat prose). Retire the constant; watch for prose tool-syntax symptoms. See [[memory/mistakes#M40]] + correction block in [[CONTEXT_PRESSURE_REPORT]] §1.
- [ ] **6. gpt-oss-120b pilot — IN PROGRESS (2026-07-10, user-approved after repeated Hermes-3 confabulations).** Handed off to an executing agent: [[GPT_OSS_PILOT_PLAN]]. Acceptance gate incl. the three 2026-07-10 confabulation cases (run 75000, QTM band structure, invented .db).
- [ ] **Update memory notes** ([[memory/infrastructure]] + MEMORY.md) with the multi-user KV table from report §3.2 answer once deployed.

---

## Knowledge beyond the local corpus (2026-07-10, post-cutover)

Context: gpt-oss's QTM band-structure answer was grounded but missed momentum-resolved tunneling — a knowledge-scope gap, not confabulation. Options 2+3 DONE (SOUL domain primers for QTM + photocurrent; grounding rules refined to allow *labeled* general-literature knowledge for conceptual questions while keeping the hard ban on invented lab-specific facts).

- [ ] **Core-papers ingestion** — create a per-sub-team "core papers" folder (data server or SharePoint) and drop the flagship PDFs (QTM: Inbar et al. Nature 2023 + key follow-ups; photocurrent: PTE/PV foundational papers; etc.). The watcher/SP sync ingests them automatically — no code needed. **USER ACTION: choose and drop the PDFs.**
- [ ] **Web-access policy decision (PI-level)** — whether to re-enable the `web`/`search` toolset for the agent (queries would leave the lab network; results feed answers). Related: [[PHASE2_BACKLOG]] B4 frontier-model access. Config change itself is one line once approved.

---

## Open Design Gaps

### Inference + Memory
- [x] **G1** — Context window budget allocation policy ✅ *decided: see INFERENCE_MEMORY.md budget table*
- [x] **G2** — Retrieval failure handling ✅ *decided: declare failure, return to user, no retries*
- [x] **G3** — Index staleness / scheduled re-indexing ✅ *decided: hash-based, schedule per source type*

### Agent Framework
- [x] **G4** — LangGraph `AgentState` schema ✅ *decided: see AGENT_FRAMEWORK.md §4*
- [x] **G5** — Cross-team synthesis pattern ✅ *decided: async fan-out via orchestrator*
- [x] **G6** — Teams message threading model ✅ *decided: keyed by conversation_id / thread_id*
- [x] **G7** — Proactive trigger list ✅ *decided: see AGENT_FRAMEWORK.md §7*

### Entire System
- [x] **G8** — System prompt design ✅ *decided: see AGENT_FRAMEWORK.md §8*
- [x] **G9** — MVP scope ✅ *decided: QTM + Photocurrent, Phase 1 read-only, Phase 2 write*
- [x] **G10** — Researcher onboarding plan ✅ *decided: see AGENT_FRAMEWORK.md §10*
- [x] **G11** — Failure and recovery ✅ *decided: see AGENT_FRAMEWORK.md §11*

---

## 1. DGX Setup
`→ see DGX_SETUP.md`

- [x] Hardware + OS readiness check ✅
- [x] vLLM installation and GPU validation ✅ *(vLLM 0.22.1, GPU visible — serving blocked, see below)*
- [x] Model pull and quantization (Hermes 3 70B AWQ INT8) ✅ *(downloaded 39.8 GB to `~/qnoe-agent/models/hermes-3-70b-awq`)*
- [x] vLLM serving ✅ *(running at localhost:8000, awq_marlin, 32K context; `python3.12-dev` installed 2026-06-08)*
- [x] Inference benchmark ✅ *(baseline run 2026-06-08, score 3.53/5 — see `benchmark/benchmark_scores.md`)*
- [x] Qdrant deployment ✅ *(7 RAG collections created: group-wide + 6 sub-teams; prose/code split dropped)*
- [x] SQLite deployment ✅ *(`events` + `audit_log` tables; LangGraph checkpointer deferred to agent framework)*
- [x] Network mounts ✅ *(NOE share pre-mounted at `/ICFO/groups/NOE`)*
- [x] Agent OS account ✅ — `qnoe-ai` created by IT 2026-06-09; owns `/opt/qnoe-agent/`
- [x] **Migrate from `~/qnoe-agent/` to `/opt/qnoe-agent/`** ✅ *(2026-06-09)*
- [x] Docker group + NVIDIA container runtime ✅ *(2026-06-09)*
- [x] **OpenShell installation** ✅ *(v0.0.59, 2026-06-11)*
- [x] **OpenShell gateway + providers** ✅ *(local-vllm provider registered, 2026-06-11)*
- [x] **Dockerfile + sandbox-policy.yaml** ✅ *(qnoe-agent:latest built, sandbox tested, 2026-06-11)*
- [x] **Systemd services** (vllm + gateway) ✅ *(vllm.service + openshell-gateway.service enabled and running, 2026-06-12)*
- [x] Open shell environment (manual `.bashrc` approach) ~~superseded by OpenShell~~
- [x] ~~**Enable qnoe-agent.service**~~ — **SUPERSEDED** by Hermes Agent. Old LangGraph service killed + disabled (2026-07-03). Now using `qnoe-hermes.service`.

**Status:** Infrastructure complete. vLLM + Qdrant running as systemd services. Hermes gateway running as `qnoe-hermes.service`.

---

## 2. Agent Framework Design
`→ see AGENT_FRAMEWORK.md`

- [x] LangGraph project scaffold ✅ *(`/opt/qnoe-agent/agent/`, 2026-06-12)*
- [x] `AgentState` TypedDict ✅ *(`agent/state.py`)*
- [x] Orchestrator node + routing logic ✅ *(`agent/graph.py`)*
- [x] QTM-Agent + Photocurrent-Agent nodes ✅ *(Phase 1 scope — other 4 deferred)*
- [x] System prompts for all agents ✅ *(`agent/prompts.py`)*
- [x] LLM client (vLLM OpenAI-compat) ✅ *(`agent/llm.py`)*
- [x] Episodic store (SQLite L3) ✅ *(`agent/episodic.py`)*
- [x] RAG retrieval (Qdrant + nomic-embed) ✅ *(`agent/retrieval.py`)*
- [x] `/switch`, `/help`, `/new` command handlers ✅
- [x] Conversation rolling window + auto-summarisation ✅
- [x] Session persistence (SqliteSaver checkpointer) ✅
- [x] Teams connector (MSAL + Graph API polling) ✅ *(`agent/teams.py` — awaiting credentials)*
- [x] Entry point ✅ *(`agent/main.py` — dev REPL mode + Teams mode)*
- [x] End-to-end test passing ✅ *(LLM responds, routing works, commands work)*
- [x] **Wire Teams credentials** ✅ *(2026-06-19 — all 4 env vars in `teams.env`: client ID `108a03c5`, tenant ID `f78a768a`, username + password. No MFA confirmed by IT.)*
- [ ] Permission tier enforcement (T2–T4) — Phase 2
- [ ] Approval flow via Teams — Phase 2
- [ ] Soft-delete wrapper — Phase 2
- [ ] Audit logger (full T2–T4 path) — Phase 2

- [x] **Agent service deployed** ✅ *(2026-06-30 — Docker container, Teams polling, end-to-end response working)*
- [x] **File access tools** ✅ *(2026-06-30 — `read_file` + `list_directory` + `search_files` via vLLM tool-calling)*
- [x] **Hermes Agent migration** ✅ *(M1–M7 complete — see §5 below; M8 cleanup remaining)*
**Status:** Phase 1 MVP operational (Hermes Agent). Migration M1–M7.5 complete (includes per-user profile routing). M8 cleanup in progress. See `MIGRATION_PLAN.md`.

---

## 3. Inference + Memory Model
`→ see INFERENCE_MEMORY.md`

### L1 — Qdrant RAG
- [x] nomic-embed-text-v1.5 deployed ✅ *(2.22 GB, vector dim 768 verified)*
- [x] ~~CodeBERT embedding model~~ — dropped; nomic-embed handles code well enough
- [x] 7 Qdrant RAG collections created ✅ *(prose/code split dropped — nomic-embed used for all content)*
- [x] QCoDeS scanner: `qcodes_scanner.py` — dedicated `qcodes-runs` collection + `qcodes_registry` table ✅ *(code written; refactored 2026-06-19 — async, incremental, mount guard, stat-based fingerprint)*
- [x] Add `qcodes-runs` to `AGENT_COLLECTIONS` in `prompts.py` ✅ *(already in code)*
- [x] Create `qcodes-runs` Qdrant collection on DGX and run initial scan ✅ *(74,760 points from 57 DBs)*
- [x] Notebook QCoDeS scan completion ✅ *(2026-06-22 — 64 DBs, 75,242 runs)*
- [x] **QCoDeS full rescan** ✅ *(2026-06-30 — 75 DBs, 75,477 runs. +18 DBs, +717 runs. Fixed: `find` timeout removed, exclusions unified via `excluded.py`)*
- [x] Verify summary cards surface in RAG queries ✅ *(2026-06-22 — QCoDeS cards returned correctly for "gate voltage sweeps" query, score 7.46; generic queries filtered by reranker threshold — BM25 will help)*
- [x] **SMB3 watcher daemon** ✅ *(2026-06-30 — deployed, all 14 acceptance tests pass. 3 bugs fixed: SubfolderManager orphaned threads, MountMonitor lazy-unmount detection, .txt removed from extensions. Cache: 37K files.)*
- [x] Ingestion pipeline (Docling, CodeSplitter, IPYNBReader, QCoDeS extractor) ✅ *(2026-06-23 — `agent/ingest/run_ingest.py` + `agent/ingest/splitter.py` + `agent/ingest/qcodes_scanner.py`; all sources ingested: 41 GitHub repos, full server scan, 75,242 QCoDeS runs)*
- [x] Scheduled re-indexing cron jobs (hash-based) ✅ *(nightly cron at 02:00 via `agent/indexing/nightly_run.py`; permission fix applied 2026-06-23)*
- [x] **Orphan sweep:** ✅ *(2026-06-19 — `sweep_orphans()` in `run_ingest.py` + `task_orphan_cleanup()` in nightly run; 7-day grace period via `missing_files` table to avoid false positives from transient mount failures)*
- [x] **Orphan cleanup double-scan bug fixed** ✅ *(2026-07-08 — crontab had `AGENT_DATA_DIR=/home/yzamir/qnoe_server_data` causing both repo_db and server_db to resolve to the same file. Fixed: removed `AGENT_DATA_DIR` override, added `SERVER_DATA_DIR=/home/yzamir/qnoe_server_data` instead. Repo DB now at `/opt/qnoe-agent/memory/episodic.db`, server DB at `/home/yzamir/qnoe_server_data/episodic.db`.)*
- [x] **Notebook folder ingested:** ✅ *(W4 worker completed — 34,894 files, 380,582 chunks)*
- [x] **Docling re-run — Papers & Books (W6):** ✅ *(2026-06-18 — 65 confirmed papers re-indexed with Docling via `--file-list /tmp/confirmed_papers_books.txt`)*
- [x] **OCR — 1-chunk files from Docling re-runs (W2 + W12):** ✅ *(2026-06-18 — 1 file: `conductivity_nonlocal.pdf`; re-indexed with `DOCLING_OCR=1`; still 1 chunk — content is genuinely short)*
- [x] **OCR — 1-chunk files from Docling re-runs (W6):** ✅ *(2026-06-24 — 7,929 unique files (7,701 PDFs + 228 short scripts). Same pattern as empty-PDF investigation: instrument drawings, CAD files, matplotlib plots. OCR won't help. Skipped.)*
- [x] **OCR — 10,873 "empty" PDFs:** ✅ *(2026-06-19 — GPU OCR run completed: 10,027+10,872 files processed, 1 chunk total. Investigation: these are matplotlib/instrument-generated single-page plots, not scanned documents. Axis labels only. Decision: do not index. See PHASE2_BACKLOG.md §B6 for VLM figure description approach.)*
- [x] Retrieval function + cross-encoder reranker ✅ *(ms-marco-MiniLM-L-6-v2, CPU, ~50ms for 20 candidates)*
- [x] RAG evaluation (20 test queries) ✅ *(2026-06-22 — 17/20 queries returned relevant results (85%). 3 failures are too-generic queries below reranker threshold; BM25 hybrid search will improve. Top scores 4.0–8.3.)*

### L2 — BM25 hybrid search
- [x] fastembed installed in both venvs (`venv/bin/pip3`, `hermes-venv/bin/pip3`) ✅ *(2026-07-06)*
- [x] BM25 model pre-cached on DGX (`~/.cache/fastembed/`, both venvs) ✅ *(2026-07-06)*
- [x] `embed_sparse()` added to `agent/ingest/embed.py` ✅ *(2026-07-06)*
- [x] `_upsert_chunks` updated to store sparse + dense vectors in all ingestion paths ✅ *(2026-07-06 — run_ingest.py, sharepoint_sync.py, qcodes_scanner.py)*
- [x] `_ensure_collection` updated to create new collections with `text-sparse` sparse field ✅ *(2026-07-06)*
- [x] Schema migrated: `text-sparse` field added to all 8 existing collections via `create_vector_name` ✅ *(2026-07-06)*
- [x] Hybrid query (dense + BM25 prefetch → RRF fusion) implemented in `hermes/plugins/qnoe_rag/__init__.py` ✅ *(2026-07-06)*
- [x] `agent/indexing/backfill_sparse.py` written (resumable, SQLite progress tracking) ✅ *(2026-07-06)*
- [x] **Run backfill** ✅ *(2026-07-09 — All 10 collections done: group-wide 634,518 pts, qcodes-runs 97,009 pts, qed 19,472 pts, photocurrent 10,236 pts, qsim 5,498 pts, qtm 505 pts, superconductivity 461 pts, xchiral/episodic_memory/mem0migrations 0 pts. Bugs fixed: empty sparse vector filter + batch size 200→50.)*
- [x] **Verify backfill complete** ✅ *(all collections show completed_at in sparse_backfill table)*
- [x] **Run the 3 previously failing exact-term queries** ✅ *(2026-07-09 — BM25 verified: "SpectroMag" returns SpectroMag manual; "scan_specific_dbs" returns database content; hybrid RRF fusion working)*
- [x] **Re-enable nightly cron** ✅ *(2026-07-09 — removed `#DISABLED_TONIGHT ` prefix)*
- [x] **Run nightly tasks manually once** ✅ *(2026-07-09 — ran; task_process_change_queue timed out at 1h (expected backlog); ran separately as parallel 6-worker job, completed 2026-07-09 17:25)*

### L3 — SQLite episodic
- [x] `events` table ✅
- [x] `audit_log` table ✅
- [x] ~~Event logger + episodic context query~~ — **superseded by Mem0 (L3.5)**. `log_event` + `get_episodic_context` exist in `agent/episodic.py` but are wired to dead LangGraph code. Hermes handles cross-session recall via Mem0; within-session via rolling window. `audit_log` table still needed for Phase 2 write permissions — see T2–T4 items above.

### L3.5 — Mem0 user memory *(built on branch `feature/mem0-per-user`; see ⏳ PENDING DEPLOY at top + [[MEM0_INTEGRATION]])*
- [x] `pip install mem0ai` ✅ *(2.0.11 in hermes-venv, 2026-07-08 — note: downgraded protobuf 7→6)*
- [x] `episodic_memory` Qdrant collection created ✅ *(768-dim)*
- [x] `user_id` keyword index created on collection ✅
- [x] Mem0 configured (local Qdrant + vLLM + nomic-embed) ✅ *(inside `qnoe_rag`, not a separate provider — avoids exclusive-slot conflict)*
- [x] `memory.search()` integrated into turn loop ✅ *(`prefetch()`; mem0 2.x API: `filters={"user_id":..}`/`top_k=`)*
- [x] `memory.add()` integrated into turn loop ✅ *(`sync_turn()`, backgrounded)*
- [x] Per-user isolation tested ✅ *(validated offline — other user gets 0 results)*
- [ ] Cross-session recall tested — **pending deploy** (needs vLLM/live agent)
- [ ] **Deploy** (`deploy_mem0.sh`) + `add(infer=True)` LLM-distillation test — **pending vLLM window** (see top)

### L4 — Skill registry
- [ ] Skill format spec + Python loader
- [ ] Nbandstructure ported as first skill
- [ ] GRASP-TWINS ported as second skill

### L5 — Knowledge graph (Phase 2, deferred)
- [ ] KùzuDB deployment
- [ ] Entity extraction pipeline
- [ ] Graph-augmented retrieval

**Status:** Designed, not started

---

## Milestone plan

| Phase | Deliverable | Acceptance criteria | Depends on |
|---|---|---|---|
| 0 | DGX configured, Hermes 70B serving at 32K | vLLM health check passes | DGX_SETUP.md |
| 1 | MVP — Orchestrator + QTM + Photocurrent, T0/T1 | All 10 acceptance criteria in G9 §9.4 | Phase 0 + L1 |
| 2 | Write access — T2/T3/T4 with approval gates | Approval flow end-to-end; soft-delete; audit log | Phase 1 |
| 3 | All 6 sub-agents, full RAG index | All sub-team repos indexed; routing correct | Phase 1 |
| 4 | Mem0 user memory (L3.5) | Cross-session recall working; per-user isolation verified | Phase 1 |
| 5 | BM25 hybrid search (L2) | Exact-term queries improve vs L1 baseline | Phase 1 |
| 6 | Skill registry (L4) | Skills callable; injected into system prompt | Phase 3 |
| 7 | Phase 2 capabilities (measurement MCPs, L5 graph) | TBD | Phase 6 |

---

## 5. Hermes Agent Migration
`→ see MIGRATION_PLAN.md, HERMES_AGENT_COMPARISON.md`

**Decision (2026-06-30):** Replace the custom LangGraph agent layer with Hermes Agent (v0.17.0, MIT license). The infrastructure (vLLM, Qdrant, watcher, ingestion, nightly indexing) stays untouched. Only the agent conversation loop, tool dispatch, memory, skills, and system prompt assembly change.

**Key gains:** persistent memory (MEMORY.md/USER.md), self-improving skills, 90+ built-in tools, context compression, gateway messaging, active community maintenance.

### Phase M1 — Install & Configure
- [x] Install Hermes Agent in separate venv (`/opt/qnoe-agent/hermes-venv/`) ✅
- [x] Create directory structure (`/opt/qnoe-agent/hermes/`) ✅
- [x] Configure `config.yaml` for local vLLM (`custom_providers`, 32K context) ✅
- [x] Verify basic operation (Hermes → vLLM → response) ✅
- [x] Patch `MINIMUM_CONTEXT_LENGTH` 64K → 16K ✅

### Phase M2 — Create Profiles
- [x] Orchestrator SOUL.md + MEMORY.md ✅
- [x] QTM SOUL.md + MEMORY.md ✅
- [x] Photocurrent SOUL.md + MEMORY.md ✅
- [x] All 3 profiles visible in `hermes profile list` ✅

### Phase M3 — RAG Plugin
- [x] Create `plugins/qnoe_rag/` plugin (user plugin dir, not nested under memory/) ✅
- [x] Port retrieval logic (Qdrant + nomic-embed + cross-encoder reranker) ✅
- [x] Implement `QnoeRagProvider(MemoryProvider)` — prefetch, queue_prefetch, system_prompt_block, rag_search tool ✅
- [x] Per-profile collection routing via `agent_identity` → `PROFILE_COLLECTIONS` map ✅
- [x] Test: plugin discovery, tool schemas, retrieval, prefetch, MemoryManager integration ✅
- [x] Install missing `einops` dep in hermes-venv ✅
- [x] Configure `memory.provider: qnoe_rag` in config.yaml ✅

### Phase M4 — QCoDeS Tool
- [x] Create `plugins/qnoe_qcodes/` standalone plugin ✅
- [x] Port SQLite query logic from `qcodes_registry` (sample, experiment, date range, free-text) ✅
- [x] Fix: DB path is `episodic.db` not `manifest.db`, default `AGENT_DATA_DIR=/home/yzamir/qnoe_server_data` ✅
- [x] Fix: timestamps are Unix epoch (TEXT column) — added epoch↔ISO conversion ✅
- [x] Enable plugin via `plugins.enabled` + `qnoe-lab` toolset in config.yaml ✅
- [x] Test: sample search, date range, free-text — all working (75,994 runs) ✅

### Phase M5 — Teams Polling Adapter
- [x] Create `plugins/teams_polling/` plugin (flat under plugins/, kind: platform) ✅
- [x] Port polling logic from `teams.py` (MSAL ROPC auth, Graph API, dedup, rate limiting) ✅
- [x] Implement `BasePlatformAdapter` interface (connect, disconnect, send, get_chat_info, handle_message) ✅
- [x] Register via `ctx.register_platform()` — Platform("teams_polling") dynamic enum member ✅
- [x] Configure gateway: `plugins.enabled` + `gateway.platforms.teams_polling` in config.yaml ✅
- [x] Test: plugin discovery, adapter instantiation, platform registration ✅
- [x] **End-to-end Teams test** ✅ *(completed during LangGraph deployment — SETUP_LOG §L; re-verified in Hermes M7 cutover)*

### Phase M6 — Multi-Agent Routing
- [x] Configure delegation settings in config.yaml (max_iterations=25, depth=1, concurrent=2) ✅
- [x] Update orchestrator SOUL.md with delegation instructions + sub-team context blocks ✅
- [x] Verify `delegate_task` available in `hermes-cli` toolset; subagents stripped of delegation/memory/clarify ✅
- [x] Test RAG routing: targeted collection queries (score 7.5), all-collections queries, prefetch (2.7K chars) ✅
- [x] Key finding: `delegate_task` doesn't load profiles — sub-team context passed via `context` param ✅

### Phase M7 — Deployment & Cutover
- [x] Start script: `start_hermes.sh` — runs `hermes gateway run` natively (no Docker) ✅
- [x] Systemd service: `qnoe-hermes.service` (User=qnoe-ai, Restart=on-failure) ✅
- [x] Bundled plugin fix: copied `teams_polling` to `site-packages/plugins/platforms/` (Platform enum needs bundled path for config parsing) ✅
- [x] Cutover: old `qnoe-agent` stopped+disabled, `qnoe-hermes` enabled+running ✅
- [x] Teams auth: MSAL ROPC succeeded, adapter connected, gateway polling ✅
- [x] Smoke test: send Teams message → get response ✅ *(SETUP_LOG §W — Teams auth, adapter connected, gateway polling)*
- [x] **M7.6 Full feature smoke test** ✅ *(2026-07-02 — 15/15 tests pass: vLLM inference, tool-calling, Qdrant RAG, QCoDeS registry, file read/list/search on CIFS, MEMORY.md persistence, SOUL.md personas, watcher, nightly cron, gateway→vLLM (after provider fix), memory save+recall, context compression, skill creation)*

### Phase M8 — Cleanup & Documentation
- [x] Archive old LangGraph code (killed `agent.main` PID 306945 on 2026-07-03; `qnoe-agent.service` disabled) ✅
- [x] Update HANDOFF.md ✅ *(2026-07-03 — architecture, milestone table, agent architecture section)*
- [x] Update AGENT_CODE_GUIDE.md ✅ *(2026-07-03 — complete rewrite for Hermes architecture)*
- [x] Update HOME.md ✅ *(2026-07-03 — active workstream)*
- [x] Update DGX_SETUP.md — add Hermes service setup steps ✅ *(2026-07-03 — §13 with 8 subsections)*
- [x] **Migration audit** ✅ *(2026-07-08 — `MIGRATION_AUDIT.md`: 7 lost capabilities identified, 8 config drift items, 8 dead files archived to `archive/langgraph/`)*
- [x] **Dead code archived** ✅ *(2026-07-08 — `graph.py`, `llm.py`, `main.py`, `prompts.py`, `state.py`, `teams.py`, `tools.py`, `retrieval.py` moved to `archive/langgraph/`)*
- [x] **Config drift synced** ✅ *(2026-07-08 — repo now matches DGX: per-profile config.yaml files, tool_use_enforcement, disabled_toolsets, compression, multiplex_profiles, user_profiles.yaml, QCoDeS run_details/diff tools)*
- [x] **L1 tool_use_enforcement fixed** ✅ *(2026-07-08 — set `true` on QTM + Photocurrent profiles, DGX + repo)*
- [x] **L2 TOP_K regression fixed** ✅ *(2026-07-08 — changed back to 3 in qnoe_rag plugin, DGX + repo)*
- [x] **Path validation restored** ✅ *(2026-07-08 — explicit ALLOWED_ROOTS instructions added to all 3 SOUL.md files with "Do NOT access" directive. Soft enforcement only — hard enforcement via plugin deferred to Phase 2)*

### Known Issues & Post-Launch Fixes

#### Priority: HIGH
- [x] **I5 — Nightly daemon health check** ✅ *(2026-07-03 — watcher healthy; cron log dir had wrong group `root`→`qnoe-ai`; snapshot pruning datetime bug fixed)*
- [x] **I5b — Verify nightly cron produces logs** ✅ *(2026-07-07 — logs confirmed)*
- [x] **I5c — Verify SharePoint delta sync in nightly cron** ✅ *(2026-07-10 — task_sync_sharepoint ran OK in 2.3s, status ok, 0 errors, 0 warnings. 0 processed is correct — full sync was Jul 9, no changes in <24h. Auth working, delta link stored.)*
- [x] **I3 — Agent can't read the server** ✅ *(2026-07-03 — NOT a permissions issue. Both CIFS mounts are readable by qnoe-ai. Root cause: same as "Tool calling as text" — model outputs `read_file(path="...")` as plain text instead of structured tool calls. Fixed by setting `tool_use_enforcement: true`. Needs service restart to take effect.)*

#### Priority: MEDIUM
- [ ] **I1 — Context compaction too frequent** — IN PROGRESS (2026-07-03). Applied fixes:
  - `compression.threshold: 0.75` (all 3 profiles) — compacts at ~24K not ~16K
  - `tool_use_enforcement: true` (all 3 profiles)
  - `tools.tool_search.enabled: 'on'` (all 3 profiles)
  - `disabled_toolsets: [tts, session_search, todo, cronjob, delegation, image_gen]` (all 3 profiles) — saves ~3,351 tokens
  - Orchestrator SOUL.md trimmed 817→423 words (removed delegation context blocks, delegation examples, failure handling)
  - RAG `TOP_K`: 5→3 in `qnoe_rag/__init__.py` — saves ~1,200 tokens
  - Fresh session baseline after changes: **~14,500 tokens** (from 17,015 before). Still ~57% overhead. Next: test Tool Slimmer (v0.6.5 on Hermes v0.17.0 — compatibility unconfirmed).
  - **Context breakdown (fresh QTM session):** tool schemas ~6,905 tok · RAG prefetch ~3,600 tok · SOUL.md ~720 tok · Hermes framing ~500 tok · history=0
  - **Tool Slimmer research:** exists ([alias8818/hermes-tool-slimmer](https://github.com/alias8818/hermes-tool-slimmer) v0.6.5), last tested on Hermes v0.15.1 (checked 2026-07-06), no v0.17 support yet. Cannot run alongside native Tool Search — must choose one.
  - **[ ] Weekly check (ongoing):** Re-check Tool Slimmer releases each week until v0.17.x is listed in release notes. When supported: disable native Tool Search, install, verify token savings. Last checked: 2026-07-08 — Hermes Atlas shows max supported version is v0.14.0 (three minor versions behind). Still not usable.
- [ ] **I2 — Some tools not used (e.g. online search)** — Test which built-in Hermes tools are available and functional. Verify web search, file tools, etc. Identify tools that aren't working and fix or disable.
- [x] **I7 — "No home channel is set for Teams_Polling" warning** ✅ *(2026-07-03 — Added `TEAMS_POLLING_HOME_CHANNEL` env var to `start_hermes.sh` with Yuval's DM chat ID. Source: `gateway/run.py:9307` checks `_home_target_env_var()` → `TEAMS_POLLING_HOME_CHANNEL`. Needs service restart to take effect.)*
- [x] **Tool calling as text** ✅ *(2026-07-03 — Root cause: `TOOL_USE_ENFORCEMENT_MODELS` in `prompt_builder.py:275` only includes GPT/Codex/Gemini/Qwen/etc — not Hermes 3. With `tool_use_enforcement: auto`, the enforcement guidance was never injected. Fixed: set `tool_use_enforcement: true` in config.yaml. Also compounds with I1 context bloat — at 19.5K tokens the model degrades further. Needs service restart to take effect.)*

#### Priority: NEW FEATURES
- [ ] **I8 — Channel @mention support** — Ask IT to add `ChannelMessage.Read.All` to app `108a03c5` (in addition to `ChannelMessage.Send` already requested). Enables bot to respond to @mentions in Teams channels. Requires polling channel messages in `teams_polling` plugin. Defer until after `ChannelMessage.Send` is granted and channel reporting is live.
- [~] **I4 — SharePoint access + embedding** — LIVE (2026-07-03). Two sites indexed: `twisted-materials` (QTOM, SpectroMag, THz gas laser) + `noe-group` (all). Delta sync every 30 min via `SharePointPoller`. Nightly full sync as safety net. ONGOING: monitor delta sync health, verify nightly runs, expand site/folder coverage as needed.
  - [x] **Full sync completed** ✅ *(2026-07-09 — 953 indexed, 76,621 skipped, 0 errors. Key fixes shipped during this run: JSONL streaming cache (no OOM), memory-aware submission guard (psutil, 20 GB floor), Docling worker kill-on-timeout fix, `--keep-cache` flag. Commits: 31b2208, 82d4798, 308c288, 0267c19)*
  - [x] **Spot-check SharePoint point in Qdrant** ✅ *(2026-07-09 — point ID 000020f6-c3a1-4cf5-9041-de71f5eceaa5, repo=noe-group, text-sparse present with 68 nnz)*
  - [x] **SP nightly task re-enabled** ✅ *(2026-07-09 — `task_sync_sharepoint` uncommented in `nightly_run.py`, deployed to DGX)*
  - [x] **`ingest_sp_qcodes.py`** ✅ *(2026-07-09 — 248 DBs, 11,905 new runs ingested into `qcodes-runs`. Rewrote to stream from JSONL cache instead of loading all items; added memory guard + `_SharedToken` refresh.)*
- [~] **I9 — `find_file` unified CIFS + SharePoint file search** — DEPLOYED (2026-07-10). New `qnoe_files` plugin exposes `find_file(query, source?, limit?)` — local SQLite `LIKE` over ingestion manifests (CIFS `index_manifest` + SP `sp_manifest`). Added `web_url` column to `sp_manifest` (written on every sync going forward) and backfilled all 22,102 existing rows from Qdrant payloads via `agent/indexing/backfill_sp_weburl.py`; wired that backfill into nightly `task_sync_sharepoint` as a safety net. Handler tested against live DBs (all 3 source modes OK); agent restarted clean (NRestarts=0).
  - [ ] **HUMAN TEST — Teams round-trip** *(couldn't be done by Claude — can't send Teams msgs; agent runtime logs are journald-only)*: DM the bot e.g. *"find the AMC300 manual"* (expect a SharePoint web link) and a CIFS-file query (expect a filesystem path). Confirms the LLM actually **invokes** `find_file` end-to-end, not just that the handler works.
- [x] **Nightly report → Teams channel** ✅ *(2026-07-08 — `agent/reporting/post_report.py` wired into nightly_run.py; posts to "Agent Logs" channel in QNOE-Agent team. Supports channel (REPORT_TEAM_ID + REPORT_CHANNEL_ID) with DM fallback. Switched from DM to channel 2026-07-08.)*
- [x] **I6 — QCoDeS run details & diff tools** ✅ *(2026-07-06)* — Added `qcodes_run_details` and `qcodes_run_diff` to `qnoe_qcodes` plugin. Both parse `description_json` in Python (not LLM) to extract swept/measured params with labels+units. Diff shows only_in_a / only_in_b / in_both for swept and measured separately. No CIFS access needed — queries `qcodes_registry` only. Deployed to `/opt/qnoe-agent/hermes/plugins/qnoe_qcodes/__init__.py`. Smoke tested against real registry (75,994 runs). **Needs service restart to activate.**

---

## 4. Benchmark — Full Stack Re-run

Re-run `benchmark/run_benchmark.py` after the full stack is operational. The baseline benchmark (2026-06-08) was run with no system prompt, no RAG, and no tools — score was 3.53/5 (marginal pass). Results in `benchmark/benchmark_scores.md`.

**Trigger:** Run this after Phase 1 is complete (RAG indexed, system prompts active, tools available).

Extend the benchmark for the full stack re-run:
- [ ] Add system prompt (per-agent persona + lab context) to all 5 tasks
- [ ] Add RAG context injection (retrieve relevant chunks before each prompt)
- [ ] Add tool use test — verify model calls tools with correct JSON when available
- [ ] Re-score all 5 tasks on same C/R/H rubric
- [ ] Compare against baseline; if T1 (code review) still < 3.5 → evaluate Qwen 2.5 72B AWQ as alternative model
