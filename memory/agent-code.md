# Agent Code
*Last updated: 2026-07-08*

> Agent source files, message flow, tools, and how the pieces connect.
> Full guide: [[AGENT_CODE_GUIDE]] · Framework design: [[AGENT_FRAMEWORK]] · Migration audit: [[MIGRATION_AUDIT]]

## File Map (Current — Hermes Agent)

| File | Role |
|---|---|
| `hermes/config.yaml` | Shared Hermes config (model, toolsets, plugins, gateway) |
| `hermes/profiles/qnoe-orchestrator/` | Orchestrator profile: SOUL.md, config.yaml, memories/ |
| `hermes/profiles/qnoe-qtm/` | QTM sub-agent profile |
| `hermes/profiles/qnoe-photocurrent/` | Photocurrent sub-agent profile |
| `hermes/config/user_profiles.yaml` | Per-user → profile routing map |
| `hermes/plugins/qnoe_rag/__init__.py` | Qdrant RAG memory provider (hybrid dense+BM25) |
| `hermes/plugins/qnoe_qcodes/__init__.py` | QCoDeS registry tools (search, run_details, run_diff) |
| `hermes/plugins/teams_polling/__init__.py` | Teams Graph API polling adapter |
| `hermes/scripts/gateway_wrapper.py` | Plugin discovery bootstrap script |
| `agent/reporting/post_report.py` | Nightly report → Teams DM |
| `agent/ingest/ingest_sp_qcodes.py` | One-time SharePoint QCoDeS ingestion |
| `agent/episodic.py` | SQLite episodic store (Phase 2 audit_log) |
| `agent/teams_check.py` | Standalone Teams credential diagnostic |

## Archived (Old LangGraph — `archive/langgraph/`)

Dead code moved 2026-07-08 during migration audit:
`graph.py`, `llm.py`, `main.py`, `prompts.py`, `state.py`, `teams.py`, `tools.py`, `retrieval.py`

## Active Infrastructure (NOT agent layer — do not touch)

| Directory | Purpose |
|---|---|
| `agent/ingest/` | Ingestion pipeline (run_ingest, splitter, embed, qcodes_scanner, sharepoint_sync) |
| `agent/indexing/` | Nightly maintenance (nightly_run.py, backfill_sparse.py) |
| `agent/watcher/` | SMB3 file watcher daemon |
| `agent/reporting/` | Nightly report → Teams DM |

## Message Flow (Hermes)

1. Teams polling adapter picks up new DM
2. Gateway routes to profile via `user_profiles.yaml` (multiplex_profiles)
3. Profile loads SOUL.md + config.yaml
4. RAG memory provider prefetches context from Qdrant (hybrid dense+BM25)
5. Hermes calls vLLM with tool schemas + RAG context + user message
6. Tool-call loop (built-in tools: read_file, list_directory, search_files, terminal, etc.)
7. Response sent back via Teams

## Tool Definitions

### Built-in (Hermes)
- `read_file`, `list_directory`, `search_files` — file access (no code-enforced path validation; SOUL.md restricts to allowed paths)
- `terminal` — shell execution
- `memory` — MEMORY.md persistence
- `execute_code` — code execution
- `patch`, `write_file` — file modification
- `web_search`, `web_extract` — web access
- `vision_analyze` — image analysis
- `skill_manage`, `skills_list`, `skill_view` — skill system

### Custom (plugins)
- `rag_search(query, collection?)` — explicit RAG search
- `qcodes_search(query?, sample?, experiment?, date_from?, date_to?, limit?)` — measurement registry
- `qcodes_run_details(db_path, run_id)` — swept/measured parameter details
- `qcodes_run_diff(db_path_a, run_id_a, db_path_b, run_id_b)` — compare two runs

### Path Validation
- **Old LangGraph:** Code-enforced `ALLOWED_ROOTS` in `tools.py` (hard boundary)
- **Hermes:** SOUL.md instruction-level only (soft boundary). All profiles include explicit "Do NOT access paths outside `/ICFO/groups/NOE/` and `/opt/qnoe-agent/repos/`" instructions. No code enforcement.

## vLLM Model ID

Must use full path: `/opt/qnoe-agent/models/hermes-3-70b-awq`

## Embedding

- **Dense:** nomic-embed-text-v1.5 (768 dim), CPU, `@lru_cache(maxsize=1)` singleton
- **Sparse (BM25):** fastembed `Qdrant/bm25`, CPU-only, `@lru_cache(maxsize=1)` singleton. Cached in `~/.cache/fastembed/` (must be pre-downloaded — see [[memory/mistakes#M31]])
- Custom code: `.py` files must exist in model dir, `auto_map` in config.json uses local paths — see [[memory/mistakes#M3 — nomic-embed custom code in Docker]]
- **Caching:** All models use `@lru_cache(maxsize=1)` — singleton per process. Memory pressure from concurrent processes can evict tensors to swap.

## RAG Plugin (qnoe_rag)

- **File:** `/opt/qnoe-agent/hermes/plugins/qnoe_rag/__init__.py`
- **TOP_K = 3** (changed from 5 on 2026-07-03; regressed to 5 on 2026-07-08 during BM25 deploy; fixed back to 3 same day — see [[memory/mistakes#M34]])
- **TOP_K_PER_COLLECTION = 20** — candidates fetched per collection before reranking
- **RERANK_POOL = 20** — pool size passed to cross-encoder
- **RERANK_THRESHOLD = 0.5** — minimum score; chunks below this are dropped
- **Flow (hybrid, 2026-07-06):** embed query (dense + BM25 sparse) → hybrid Qdrant search per collection (RRF fusion of dense + sparse prefetch) → deduplicate → cross-encoder reranks top 20 → return top 3 across ALL collections combined
- **Savings from TOP_K 5→3:** ~1,200 tokens per turn

### BM25 Sparse Vectors (added 2026-07-06)

- **Why:** Dense-only retrieval fails on exact-term queries (device IDs like `SLG07-C2`, function names, paper titles). BM25 gives high weight to rare, specific tokens.
- **Library:** `fastembed` 0.8.0, model `Qdrant/bm25` (CPU-only, ~1MB, cached in `~/.cache/fastembed/`)
- **Storage:** Each Qdrant point has two vectors: unnamed dense (`""`) + named sparse (`"text-sparse"`)
- **Query:** `Prefetch(dense) + Prefetch(sparse, using="text-sparse")` → `FusionQuery(fusion=Fusion.RRF)` — all in one Qdrant call per collection
- **Schema:** All 8 collections have `text-sparse` field (added 2026-07-06 via `create_vector_name`)
- **Backfill:** `agent/indexing/backfill_sparse.py` — resumable, tracks progress in `sparse_backfill` SQLite table. Run once to populate sparse vectors for existing 638K+ points. **NOT YET RUN** as of 2026-07-08.
- **pip path:** use `pip3` not `pip` in agent venv (`/opt/qnoe-agent/venv/bin/pip3`)

## Nightly Report (agent/reporting/post_report.py)

Sends the nightly maintenance report to a Teams channel after each nightly run. Already wired into `nightly_run.py` (2026-07-08). Switched from DM to channel on 2026-07-08.

- **Input:** `/opt/qnoe-agent/logs/nightly_report.json` written by `nightly_run.py`
- **Output:** Two Teams messages — HTML summary table + separate error/warnings detail message if any
- **Delivery:** Channel (preferred) or DM (fallback)
  - **Channel:** `REPORT_TEAM_ID` + `REPORT_CHANNEL_ID` → posts to `/teams/{id}/channels/{id}/messages`
  - **DM fallback:** `REPORT_CHAT_ID` or `REPORT_TO_EMAIL` → posts to `/chats/{id}/messages`
- **Current target:** "Agent Logs" channel in QNOE-Agent team
- **Auth:** Same MSAL ROPC creds as SharePoint (`secrets/sharepoint.env`)
- **Config:** `secrets/report.env`
- **Dry-run:** `python -m agent.reporting.post_report --dry-run` prints HTML without sending

## SharePoint QCoDeS Ingestion (agent/ingest/ingest_sp_qcodes.py)

One-time script to pull QCoDeS `.db` files from SharePoint and index them into `qcodes-runs`.
**Status: NOT YET RUN** — waiting for full SP sync to complete first.

## Active Toolsets & Context Budget (QTM profile, fresh session)

**Toolset composition (2026-07-09):** changed `toolsets: [hermes-cli, qnoe-lab]` → `toolsets: [file, terminal, clarify, qnoe-lab]` in all 3 profiles (Step 2 of the context-pressure package). Core tools can NEVER be deferred by Tool Search (`_HERMES_CORE_TOOLS` in `tools/tool_search.py`), so slimming goes via toolset *composition*, not deferral. Resident tools dropped **12 → 7**: now `read_file, write_file, patch, search_files, terminal, process, clarify`. Dropped (uncallable until re-added or wrapped as deferrable plugin tools): `skill_manage/skill_view/skills_list` (skills), `memory`, `execute_code`. The `memory` *tool* ≠ the `qnoe_rag` memory *provider* (separate, untouched).

| Component | Before | After (2026-07-09) |
|---|---|---|
| Tool schemas (core) | ~6,054 (12 tools, real tokenizer) | **~3,550 (7 tools)** — measured via vLLM `/tokenize` |
| RAG prefetch (3 chunks) | ~3,600 | ~3,600 (Provence swap evaluated + rejected — see below) |
| QTM SOUL.md | ~720 | ~720 |
| Hermes framing | ~500 | ~500 |
| **Floor (empty history)** | **~11,725** | **~9,200** (−2,504 tool tokens) |

Window raised 32K → 64K (`context_length: 65536`, compaction now ~48K at threshold 0.75).

**RAG reranker:** stays **cross-encoder-msmarco** (cpu). Provence (`naver/provence-reranker-debertav3-v1`) was evaluated 2026-07-09 as a prune+rerank replacement: 72% top-3 token reduction, 20/20 answer survival, but **32.5× cpu latency (~22s/query)** on the Spark CPU — far over the ≤2× gate and past the RAG prefetch's 10s join timeout. **Not deployed.** Full eval: `logs/provence_eval.md`.

**Disabled toolsets (all profiles):** `tts`, `session_search`, `todo`, `cronjob`, `delegation`, `image_gen`

**Note:** `process` cannot be individually disabled — shares `terminal` toolset with `terminal`.
