# Decisions Log
*Last updated: 2026-07-10*

> Architectural and design decisions with reasoning. Append new entries at the bottom.
> Related: [[memory/mistakes]] · [[HANDOFF#All design decisions — summary]]

## D1 — Hermes 3 70B AWQ as base model

**Date:** 2026-06-08
**Context:** Need local LLM for lab agent on DGX Spark (128GB unified memory).
**Decision:** Hermes 3 70B, AWQ INT8 quantization (~70GB). Serves via vLLM at 32K context.
**Reasoning:** Best open-weight model at 70B for tool calling and instruction following. AWQ fits in memory with room for embedding models.

## D2 — nomic-embed-text-v1.5 for all embeddings

**Date:** 2026-06-10
**Context:** Needed embedding model for RAG. Initially considered CodeBERT for code.
**Decision:** Single model (nomic-embed) for both prose and code. Dropped prose/code collection split.
**Reasoning:** nomic-embed handles code well enough. Simpler architecture, one model to maintain.

## D3 — Unified exclusion list via watcher.yaml

**Date:** 2026-06-30
**Context:** Three separate exclusion lists (env var, constant, watcher config) caused scan gaps.
**Decision:** `excluded.py` reads `watcher.yaml` as single source of truth. All `find` commands use `find_prune_args()`.
**Reasoning:** Missed 18 QCoDeS databases because of inconsistent exclusions.

## D4 — Replace LangGraph with Hermes Agent

**Date:** 2026-06-30
**Context:** Custom LangGraph agent works but lacks persistent memory, skills, context compression.
**Decision:** Migrate to Hermes Agent v0.17.0. Infrastructure unchanged. Only conversation loop changes.
**Reasoning:** Built-in MEMORY.md/USER.md, self-improving skills, 90+ tools, active maintenance. See [[HERMES_AGENT_COMPARISON]].

## D5 — Separate venvs for agent and Hermes

**Date:** 2026-06-30
**Context:** Hermes requires `openai>=1.30` but agent code pins an older version.
**Decision:** `/opt/qnoe-agent/hermes-venv/` separate from `/opt/qnoe-agent/venv/`.
**Reasoning:** Avoids dependency conflicts. Both can coexist during migration.

## D6 — Server ingestion uses separate manifest DB

**Date:** 2026-06-18
**Context:** Repo ingestion and server ingestion were sharing manifest DB, causing conflicts.
**Decision:** Server uses `/home/yzamir/qnoe_server_data/episodic.db`, repos use `/opt/qnoe-agent/memory/episodic.db`.
**Reasoning:** Different data directories, different ownership, different update cadences.

## D7 — Per-user profile routing via adapter-side stamping

**Date:** 2026-07-02
**Context:** Need each Teams user to get their sub-team's SOUL.md, RAG collections, and memories automatically.
**Decision:** Adapter stamps `source.profile` on `SessionSource` based on user ID mapping in `user_profiles.yaml`. Gateway's `_profile_runtime_scope` handles the rest. Do NOT use `multiplex_profiles: true`.
**Reasoning:** Single Teams bot credential (no per-profile tokens needed). Multiplexer creates duplicate handlers when sub-profiles share config. `source.profile` alone triggers profile scope correctly.

## D8 — Config inheritance via symlinks

**Date:** 2026-07-02
**Context:** Hermes profiles don't inherit config from parent. Sub-profiles need identical model/provider/plugin config.
**Decision:** Sub-profile `config.yaml` and `.env` are symlinks to the main config. Only `SOUL.md` and `memories/` differ per profile.
**Reasoning:** Single source of truth. Edit main config once, all profiles pick it up. `hermes profile create --clone` would also work but profiles already exist with custom SOUL.md.

## D9 — Provider config: custom + explicit base_url + api_key

**Date:** 2026-07-02
**Context:** vLLM local server needs no auth, but Hermes `custom` provider requires `api_key` for auth resolver.
**Decision:** Set `provider: custom`, `base_url: http://localhost:8000/v1`, `api_key: no-key-required`, `max_tokens: 4096` in config.yaml model section.
**Reasoning:** Auth resolver requires api_key to detect provider. Dummy value works for keyless vLLM. max_tokens must be capped below vLLM's 32K context.

## D10 — Per-user profile routing: adapter-side stamping with multiplex_profiles

**Date:** 2026-07-02
**Context:** Need each Teams user routed to their sub-team's profile (SOUL.md, RAG, memory) via a single Teams bot.
**Decision:** `multiplex_profiles: true` (top-level config) + adapter stamps `source.profile` from `user_profiles.yaml` mapping. Sub-profile configs have `gateway.platforms.teams_polling.enabled: false`. Two patches to gateway internals prevent duplicate adapter creation.
**Reasoning:** Single bot credential, no per-profile tokens. Profile routing happens at adapter level, gateway's `_profile_runtime_scope` handles the rest. Required patching `config.py` and `run.py` — patches will need re-applying after hermes-agent upgrades.

## D12 — BM25 hybrid search via fastembed sparse vectors

**Date:** 2026-07-06
**Context:** 3 of 20 test queries fail because dense-only search can't match exact tokens — device IDs like `SLG07-C2`, function names, paper titles. Semantic similarity is weak for rare, specific terms.
**Decision:** Add BM25 sparse vectors (fastembed `Qdrant/bm25` model) alongside existing nomic-embed dense vectors. Qdrant's native sparse vector support + RRF fusion handles hybrid retrieval. No separate BM25 index.
**Architecture:**
- Each Qdrant point stores two vectors: unnamed dense (`""`) + named sparse (`"text-sparse"`)
- Query time: two `Prefetch` queries (dense + sparse) fused with `FusionQuery(fusion=Fusion.RRF)` in one Qdrant call per collection
- Reranking layer unchanged — cross-encoder still reranks the RRF-fused results
**Library:** `fastembed` 0.8.0 — CPU-only, ~1MB model, no GPU required
**Files changed:** `agent/ingest/embed.py`, `agent/ingest/run_ingest.py`, `agent/ingest/sharepoint_sync.py`, `agent/ingest/qcodes_scanner.py`, `hermes/plugins/qnoe_rag/__init__.py`
**New file:** `agent/indexing/backfill_sparse.py` — one-time resumable backfill for existing points
**Reasoning:** Native Qdrant hybrid avoids maintaining separate index. fastembed BM25 is trivial to deploy. Fixes exact-match failures without touching the reranking layer.

## D11 — tool_use_enforcement: true for Hermes 3

**Date:** 2026-07-03
**Context:** Hermes 3 70B outputs tool calls as plain text (e.g., `read_file(path="...")`) instead of structured JSON tool_calls. This makes the agent unable to use any tools (file read, RAG search, QCoDeS query). vLLM's `--tool-call-parser hermes` works perfectly with minimal context (359 tokens → structured tool call) but fails at 19.5K tokens.
**Decision:** Set `agent.tool_use_enforcement: true` in config.yaml (was `auto`).
**Reasoning:** The `auto` setting only injects tool-use guidance for GPT/Codex/Gemini/Qwen/DeepSeek/Grok — Hermes 3 is not in the list (`TOOL_USE_ENFORCEMENT_MODELS` in `prompt_builder.py:275`). Setting `true` forces the guidance for all models. This is safe — the guidance just tells the model to use tools instead of describing actions.

## D13 — Per-user memory via Mem0 library inside `qnoe_rag` (drop built-in MEMORY.md/USER.md)

**Date:** 2026-07-08
**Status:** DESIGNED, not yet implemented. Full program: [[MEM0_INTEGRATION]].
**Context:** Want per-*user* persistent memory (preferences, working style) across sessions. Hermes's built-in `USER.md` is per-*profile* (shared across a whole sub-team), so it cannot isolate per person. `MEMORY.md` (agent env/diary notes) is low-value for a lab RAG assistant. Also tight on context vs the ~19.5K tool-calling cliff ([[memory/decisions#D11]]).
**Decision:**
- Drop `USER.md` only: `user_profile_enabled: false` in all profile `config.yaml` files. **Keep `MEMORY.md`** (per-team static seed content; not redundant with per-user Mem0). Built-in memory is currently ON — profile configs set only `memory: {provider: qnoe_rag}` but `load_config` deep-merges `DEFAULT_CONFIG` (`memory_enabled/user_profile_enabled: True`), so a flag must be set **explicitly** to change it. (Do NOT run `hermes tools disable memory` — that kills the memory tool ecosystem.)
- Add per-user memory by calling the **Mem0 library** (`mem0ai`, oss/self-hosted mode) **from inside the existing `qnoe_rag` memory provider** — NOT as a Hermes `memory.provider` (only one allowed, `qnoe_rag` holds it).
- `qnoe_rag` stays the single injector: `prefetch()` emits Mem0 facts (`## What I remember about you`, top-3) ahead of RAG chunks; the previously no-op `sync_turn()` runs a backgrounded `mem0.add()`.
- Mem0 keyed on platform `user_id` (available in `initialize()` kwargs); dedicated Qdrant `episodic_memory` collection (768-dim); LLM = vLLM, embedder = local nomic.
**Reasoning:** Library-inside-provider avoids the exclusive-provider conflict with RAG and needs no custom memory logic (Mem0 owns fact extraction/dedup/storage). Net context ≈ **−100 tok/turn** (keep MEMORY.md, −500 USER.md +400 Mem0 facts) — the win is correctness (true per-user memory), not tokens. See [[MEM0_INTEGRATION]].
**Status update (2026-07-08):** Implemented on branch `feature/mem0-per-user` (2 commits). Validated on DGX without vLLM: mem0ai **2.0.11** installed (additive; note protobuf 7→6 downgrade), `episodic_memory` collection created, config schema + offline embedder + write/read round-trip + per-user isolation all confirmed. mem0 2.x `search()` needs `filters={"user_id":..}`/`top_k=` (code fixed). Deploy staged in `scripts/deploy_mem0.sh` — NOT applied; pending a vLLM window after the SharePoint full sync. Remaining: `add(infer=True)` LLM test + live deploy/restart.

## D14 — `find_file`: unified CIFS + SharePoint file search over ingestion manifests (not live find / Graph)

**Date:** 2026-07-10
**Context:** Users need to locate a file by name/path. The agent could `find` on the CIFS mount, but SharePoint files never touch the filesystem (streamed → embedded → deleted), so they were unfindable by name. Wanted one tool covering both stores.
**Decision:** New `qnoe_files` plugin exposing `find_file(query, source?, limit?)` that runs **local SQLite `LIKE` queries over the ingestion manifests** — `index_manifest.file_path` (CIFS: server + repo `episodic.db`) and `sp_manifest.item_path`/`web_url` (SharePoint) — and merges results. `source ∈ {all,cifs,sharepoint}`; returns a filesystem path (CIFS) or web URL (SharePoint).
**Rejected alternatives:**
- **Live `find` over CIFS** — a full-mount scan takes hours (per [[memory/ingestion#QCoDeS Scanner]]); unusable for an interactive tool.
- **Live Graph Search API for SP** — needs a token + network per query; the manifest is already synced every 30 min and is local/offline.
**Supporting change:** added a `web_url` column to `sp_manifest` (payloads already carried the URL in `source`; the manifest didn't). Written on every sync via `_record_item`; existing 22,102 rows backfilled from Qdrant payloads by `agent/indexing/backfill_sp_weburl.py` (idempotent, read-only Qdrant retrieves + SQLite update — **adds zero Qdrant points**). Backfill also wired into nightly `task_sync_sharepoint` as a safety net.
**Trade-off:** coverage = *indexed* files only (supported extensions, non-excluded, ≤ up to 30 min stale). Files never ingested (e.g. `.xlsx`, images) won't appear — acceptable; live `find`/Graph remain an optional future fallback.
**Files:** new `hermes/plugins/qnoe_files/__init__.py`, new `agent/indexing/backfill_sp_weburl.py`; changed `agent/ingest/sharepoint_sync.py`, `agent/indexing/nightly_run.py`. Deployed + backfilled 2026-07-10 (commit `2b7fd26`). Env knobs: `CIFS_MANIFEST_DBS` (`:`-sep), `SP_MANIFEST_DB`. Pending: human Teams round-trip (TODO I9).

## D15 — Production cutover to gpt-oss-120b (llama.cpp), superseding Hermes 3 / vLLM

**Date:** 2026-07-10
**Context:** The gpt-oss-120b pilot passed all gates (decode 48.8 tok/s ≈ 8× Hermes-3, structured tool calls to 32K, no confabulation). User approved cutover + raising context. See [[GPT_OSS_CUTOVER_PLAN]].
**Decision:** Make **gpt-oss-120b MXFP4 (GGUF) served by llama.cpp** the production model on `localhost:8000`, replacing Hermes 3 70B AWQ / vLLM. Keep the systemd unit name `vllm.service` (preserves the `Requires=` chain + runbooks); `ExecStart` now runs `scripts/start_llamacpp.sh`. Hermes-3 remains on disk (`models/hermes-3-70b-awq`) with `scripts/start_vllm.sh` untouched as the one-line rollback. Supersedes **[[memory/decisions#D1]]** (model choice).
**KV / concurrency:** `-c 262144 --parallel 4` **without** `--kv-unified` → 4 fixed 64K slots (262144-token pool), ≥4 concurrent users at full 64K. `--kv-unified` was rejected because it caps the pool to the model's 128K train context (shared → only ~32K/user at 4 concurrent). Measured 44-48GB RAM available while serving.
**Enforcement:** set `false` at cutover — but **REVERTED to `true` 2026-07-14** (all 3 profiles): red-team R2 found gpt-oss *does* prose-fallback on tool calls intermittently (emits the call as text instead of executing), and enforcement reduces it. The "documented remedy" fired. See `redteam/BACKLOG.md` R2.
**`reasoning_effort:low`** baked into the server (`--chat-template-kwargs`) to contain gpt-oss's empty-content trait.
**Changed in production:** `vllm.service` unit (`ExecStart`+`Description`), new `scripts/start_llamacpp.sh`, 3 profile configs (`model.default: gpt-oss-120b`, `tool_use_enforcement: false`; `context_length` already 65536), `scripts/start_hermes.sh` (`MEM0_LLM_MODEL=gpt-oss-120b`). Deployed on branch `feature/gpt-oss-cutover`.
**Validation:** all 6 cutover gates passed (health/id, coherence+speed, reasoning budget, tool calls, concurrency, acceptance). Full-agent Teams round-trips remain a human verification.

## D16 — Knowledge beyond the local corpus: domain primers + labeled general knowledge

**Date:** 2026-07-10
**Context:** gpt-oss's QTM band-structure answer was fully grounded but missed momentum-resolved tunneling — the lab's flagship concept. Not confabulation: a knowledge-scope gap. The concept exists locally only as one thesis-proposal sentence (retrieval-luck dependent), and the post-M38 grounding rules had deliberately muzzled the model's own (correct) literature knowledge.
**Decision (options 2+3 of 4; user-approved):**
- **Domain primers in SOUL.md** (QTM: momentum-conserving tunneling through the twistable moiré junction → direct E(k) mapping, Inbar et al. Nature 2023; photocurrent: PTE vs PV, gate-dependence). ~150 tokens each, always in context, immune to retrieval luck.
- **Grounding rules refined:** conceptual/textbook questions MAY use general-literature knowledge **labeled as such**; lab-specific facts (runs, files, parameters, devices, dates) remain retrieval-only — never guessed.
**Deferred to [[TODO]] (options 1+4):** per-team core-papers ingestion folders (user drops PDFs, watcher ingests — fits no-data-leaves-lab); web-access toolset re-enable (PI-level policy — queries leave the network; relates to [[PHASE2_BACKLOG]] B4).
**Related:** QCoDeS reporting rule (same day): every measurement answer must state run NAME + swept/measured parameters — enforced in SOUL + registry hook + tool output shape.

## D17 — Read-only enforcement via systemd namespace, not the OpenShell container (B7)

**Date:** 2026-07-14
**Context:** Red-team R4 (see `redteam/BACKLOG.md` Round 2b): the production agent actually appended a line to `repos/QTM-CodeBase/README.md` in 1/5 probe runs. T0/T1 read-only was SOUL-instruction-only; `write_file`/`patch`/`terminal` are resident. `B7_SANDBOX_HANDOFF.md` posed the mechanism choice: (1) OpenShell/landlock container, (2) systemd sandboxing, (3) terminal-backend-only (rejected — file tools bypass it).
**Decision: option 2 — systemd drop-in on `qnoe-hermes.service`** (`50-b7-readonly.conf`): `ReadOnlyPaths=/opt/qnoe-agent /ICFO /mnt/noe /home/yzamir`, rw carve-outs `memory/ logs/ hermes/`, `InaccessiblePaths=secrets/` with `LoadCredential=teams.env`. Mount namespace binds ALL tools (file tools + every terminal child) — same guarantee OpenShell would give for the filesystem.
**Why not OpenShell now:** the container/policy were built for the old LangGraph agent (`venv/`, `agent/` entrypoint); the Hermes gateway (hermes-venv, Mem0, Teams polling, embedded models) was never validated in it — days of re-validation vs a unit drop-in verified in one afternoon. OpenShell's real marginal value is network L7 + credential scoping → still [[PHASE2_BACKLOG]] B7 for T2–T4.
**Verification:** standing `qnoe-b7-test.service` + `scripts/b7_probe.sh` (19/19 PASS 2026-07-14) — probe unit must carry identical directives to the drop-in.
**Caveat:** harness Channel A (`hermes -z`) runs outside the unit → unsandboxed; enforcement is on the Teams-facing gateway. Details: [[memory/infrastructure#B7 Read-Only Sandbox on qnoe-hermes (2026-07-14, memory/decisions#D17)]].

## D18 — OpenShell sandbox supersedes the systemd namespace for B7 read-only enforcement

**Date:** 2026-07-14 (same day as D17 — user rejected the systemd mechanism's cons and OpenShell v0.0.82 removed the blocker)
**Context:** D17 chose systemd because OpenShell 0.0.59's Docker driver silently ignored bind mounts and the Phase-0 container was built for the LangGraph agent. Both reasons died: v0.0.82 ships working `--driver-config-json` mounts (gated by `[openshell.drivers.docker] enable_bind_mounts = true` in gateway.toml, loaded via the new `--config` flag), and the Hermes runtime was validated in-container during the migration (Stages 2–4 of `~/.claude/plans` B7-OS plan).
**Decision:** production gateway = `qnoe-hermes-sandbox.service` → `openshell sandbox create` (image `qnoe-hermes:0.1`, uid 1001, policy `config/sandbox-policy.yaml`, mounts `config/hermes-sandbox-mounts.json`). Default-deny filesystem (a forgotten path is now ABSENT, not writable — inverts the /mnt/noe failure class), landlock, L7 egress proxy with per-hostname policy (negative-tested: arbitrary hosts 403), audit trail. The systemd unit `qnoe-hermes.service` + `50-b7-readonly.conf` stay installed, disabled, as the one-command rollback (`sudo systemctl start qnoe-hermes` — Conflicts= flips the pair).
**Verified:** b7_probe 24/24 in-sandbox; Teams round-trip, Mem0 write/recall (incl. across restarts), RAG, qcodes run-848, and the R4 perm-write probe — model ATTEMPTED the write, filesystem refused (EROFS), file hash unchanged. Failure drills: openshell-gateway restart (unit self-heals ~60s), docker kill, clean stop, Conflicts-flip both ways.
**Deferred to hardening:** dedicated sandbox uid (identity isolation), OpenShell inference proxy for the LLM path, systemd drop-in retirement after weeks of stability. **Carried caveat:** red-team harness Channel A (`hermes -z`) still runs UNCONFINED on the host.
**Runtime traps solved on the way (see [[memory/mistakes#M50]]):** OpenShell forces HOME=/sandbox; landlock blocks /dev/null writes + reads of unlisted paths (incl. bind-mount targets and the container cwd); ALL egress must ride the injected L7 proxy (aiohttp needs trust_env=True; anything hardcoding localhost breaks — Mem0's Qdrant host now derives from QDRANT_URL; LLM base_url = http://host.openshell.internal:8000/v1 with a host /etc/hosts alias to 127.0.0.1 so ONE config serves all mechanisms).
