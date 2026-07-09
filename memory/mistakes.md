# Mistakes & Pitfalls
*Last updated: 2026-07-09 (M35-M36 added — context-pressure package findings)*

> Bugs fixed and hard-won technical lessons. Check here before debugging similar issues.
> Related: [[memory/deploy-patterns]] · [[memory/infrastructure]] · [[SETUP_LOG]]

## M1 — Cross-encoder safetensors permissions

**Symptom:** Permission denied loading cross-encoder model in container.
**Root cause:** `sudo cp` created files owned by root with 600 permissions.
**Fix:** `chmod 644` on all safetensors files after copying.

## M2 — nomic-embed GPU conflict

**Symptom:** CUDA OOM or model loading failure for embedding.
**Root cause:** GPU fully occupied by vLLM.
**Fix:** Force `device="cpu"` for nomic-embed. CPU inference ~50ms for 20 candidates — acceptable.

## M3 — nomic-embed custom code in Docker

**Symptom:** HuggingFace connection error in offline container.
**Root cause:** `config.json` `auto_map` referenced remote HF repo paths (`nomic-ai/nomic-bert-2048--...`). Custom Python modules not present locally.
**Fix:** Copy `configuration_hf_nomic_bert.py`, `modeling_hf_nomic_bert.py`, `__init__.py` from host HF cache into model dir. Update `auto_map` to local paths (e.g., `configuration_hf_nomic_bert.NomicBertConfig`).

## M4 — Qdrant API version mismatch

**Symptom:** `AttributeError: search` not found.
**Root cause:** Qdrant v1.18 changed API.
**Fix:** Use `query_points()` instead of `search()`.

## M5 — vLLM model ID must be full path

**Symptom:** Model not found error from vLLM.
**Fix:** Use `/opt/qnoe-agent/models/hermes-3-70b-awq` (full path), not just model name.

## M6 — QCoDeS column name

**Symptom:** SQL error querying QCoDeS databases.
**Root cause:** Column is `run_description`, not `description`.
**Fix:** Use correct column name in all QCoDeS queries.

## M7 — QCoDeS `find` timeout on CIFS

**Symptom:** Only 57 of 75 databases found. Missing 18 DBs from deep directories.
**Root cause:** `find` had 300s timeout. Full CIFS traversal takes 2h+.
**Fix:** Removed timeout entirely. Also added `Setups/`, `Personal/`, `Fabrication/` to watcher scan paths.

## M8 — Thumbs.db in CIFS find commands

**Symptom:** `find` returning Windows thumbnail cache files.
**Fix:** Add `! -iname Thumbs.db` to all find commands on CIFS mounts.

## M9 — MSAL offline_access scope

**Symptom:** Authentication error with Teams.
**Root cause:** Passing `offline_access` explicitly in MSAL scopes.
**Fix:** Don't pass it — MSAL adds it automatically.

## M10 — Container passwd file for PyTorch

**Symptom:** PyTorch initialization fails in container.
**Root cause:** No `/etc/passwd` entry for uid 1001.
**Fix:** Generate passwd file at startup, mount read-only into container.

## M11 — DGX file ownership

**Symptom:** Permission denied when agent/Hermes writes to `/opt/qnoe-agent/`.
**Root cause:** Files created by `yzamir` or `sudo mkdir` without group write.
**Fix:** Always `sudo chown -R qnoe-ai:qnoe-ai` AND `sudo chmod -R g+w` after creating files.

## M12 — tee to qnoe-ai-owned directory

**Symptom:** Log file empty after `nohup ... | tee /opt/qnoe-agent/logs/file.log`.
**Root cause:** `tee` runs as yzamir, can't write to qnoe-ai-owned directory.
**Fix:** Redirect to home directory: `> ~/file.log 2>&1`.

## M13 — Two copies of teams_polling plugin

**Symptom:** Code changes to `hermes/plugins/teams_polling/__init__.py` have no effect.
**Root cause:** Runtime loads from `hermes-venv/.../site-packages/plugins/platforms/teams_polling/__init__.py`, not from `hermes/plugins/`.
**Fix:** Always deploy to BOTH locations. SCP to `/tmp/`, then `sudo cp` to both paths.

## M14 — HERMES_HOME points to profile dir, not hermes root

**Symptom:** `user_profiles.yaml` not found at `$HERMES_HOME/config/user_profiles.yaml`.
**Root cause:** At runtime, `HERMES_HOME` = `/opt/qnoe-agent/hermes/profiles/qnoe-orchestrator/`, not `/opt/qnoe-agent/hermes/`.
**Fix:** Use `hermes_home.split("/profiles/")[0]` to get the root hermes dir.

## M15 — MessageEvent text is required positional arg

**Symptom:** `TypeError: MessageEvent.__init__() missing 1 required positional argument: 'text'`
**Root cause:** Hermes v0.17.0 changed MessageEvent to a dataclass with `text` as first required arg.
**Fix:** `MessageEvent(text=text, source=source, ...)` instead of setting attributes after construction.

## M16 — Provider name "vllm-local" unknown to auth resolver

**Symptom:** `Unknown provider 'vllm-local'` error.
**Root cause:** Hermes alias map has `"vllm": "custom"` but not `"vllm-local"`.
**Fix:** Use `provider: custom` in config.yaml.

## M17 — Custom provider max_tokens defaults to 65536

**Symptom:** `max_tokens=65536 cannot be greater than max_model_len=32768`.
**Root cause:** Custom provider uses large default max_tokens.
**Fix:** Set `max_tokens: 4096` in `model:` section of config.yaml.

## M18 — multiplex_profiles: three layers of duplication

**Symptom:** User gets 2-4 responses per message.
**Root causes (all three independent):**
1. **Old LangGraph agent still running** — `agent.main` (PID 306945 from Jun 30) was polling the same Teams bot alongside Hermes. Fix: `sudo kill -9`, `sudo systemctl disable qnoe-agent.service`.
2. **Plugin auto-enable overrides `enabled: false`** — `gateway/config.py` line ~2150 unconditionally sets `enabled = True` for plugins whose `check_fn()` returns True (env vars present).
3. **"default" profile creates duplicate adapter** — `profiles_to_serve(multiplex=True)` returns `"default"` alongside the active profile name, creating a second adapter.
**Fix (upgrade-safe):** Added `self.bot_token = self._username` to teams_polling adapter. The gateway's `_adapter_credential_fingerprint()` checks `bot_token` and deduplicates adapters with the same credential. No gateway patches needed — all reverted. Only our own plugin code is modified.
**Lesson:** Always prefer fixing in YOUR code over patching framework internals.

## M19 — Profile secret scope isolates from process env

**Symptom:** `No inference provider configured` when routing to sub-profile.
**Root cause:** `_profile_runtime_scope` sets isolated secret scope via `set_secret_scope()`. Sub-profile `.env` must contain all needed vars — `get_secret()` does NOT fall through to `os.environ`.
**Fix:** Sub-profiles need `.env` with infra vars, OR use `api_key`/`base_url` directly in config.yaml `model:` section.

## M20 — multiplex_profiles must be top-level in config.yaml

**Symptom:** `source.profile` ignored, all messages go to orchestrator.
**Root cause:** `load_gateway_config()` line 852 checks `yaml_cfg.get("multiplex_profiles")` at top level only. Nesting under `gateway:` has no effect.
**Fix:** Put `multiplex_profiles: true` at the root of config.yaml, not under `gateway:`.

## M21 — Teams HTML entities break /new command

**Symptom:** `/new` command not recognized: `Unrecognized slash command /new&nbsp;`
**Root cause:** Teams wraps message content with `&nbsp;` HTML entities.
**Fix:** Added `import html as _html` and `_html.unescape()` to `_strip_html()` in teams_polling adapter.

## M22 — Plugin discovery uses profile dir, not hermes root

**Symptom:** RAG plugin not loaded — no memory prefetch, no tool calls for RAG.
**Root cause:** `get_hermes_home() / "plugins"` returns the PROFILE dir (e.g., `profiles/qnoe-orchestrator/plugins/`), not `hermes/plugins/`. Plugin discovery runs once at startup and is cached.
**Fix:** Create symlinks from each profile's `plugins/` dir to the hermes root plugins dir: `sudo -u qnoe-ai ln -s /opt/qnoe-agent/hermes/plugins /opt/qnoe-agent/hermes/profiles/qnoe-*/plugins`. Must use `sudo -u qnoe-ai` because profile dirs are 700 owned by qnoe-ai.

## M23 — Hermes 3 outputs tool calls as text instead of structured JSON

**Symptom:** Agent writes `read_file(path="...", limit=500)` as plain text instead of producing a structured `tool_calls` response. vLLM's `--tool-call-parser hermes` never sees the call. Agent appears unable to use tools.
**Root cause:** Hermes Agent's `tool_use_enforcement` config was `auto`. The auto list (`TOOL_USE_ENFORCEMENT_MODELS` in `prompt_builder.py:275`) includes GPT, Codex, Gemini, Qwen, DeepSeek, Grok — but NOT Hermes 3. Without the enforcement guidance in the system prompt, the model defaults to writing tool invocations as prose.
**Compounding factor:** Context bloat (19.5K tokens) makes the model even less likely to produce structured tool calls. At 359 tokens (direct curl test), vLLM produces perfect structured `tool_calls` with `finish_reason=tool_calls`.
**Fix:** Set `tool_use_enforcement: true` (not `auto`) in `config.yaml` under `agent:`. This injects the enforcement guidance for ALL models.
**Investigation path:** `agent/system_prompt.py:230` → checks `_tool_use_enforcement` → `auto` matches against `TOOL_USE_ENFORCEMENT_MODELS` → no match for "hermes" → guidance not injected.
**Lesson:** When using a local/custom model with Hermes Agent, always set `tool_use_enforcement: true` — the auto-detection only covers major commercial model families.

## M24 — Nightly cron log dir wrong group ownership

**Symptom:** No `nightly_reindex.log` file ever created. Nightly cron appears to never run.
**Root cause:** `/opt/qnoe-agent/logs/` had `group: root` (not `qnoe-ai`). Cron runs as `yzamir` (in group `qnoe-ai`), so `>> /opt/qnoe-agent/logs/nightly_reindex.log` fails silently. When shell redirect fails, the command doesn't execute.
**Fix:** `sudo chown qnoe-ai:qnoe-ai /opt/qnoe-agent/logs/`
**Lesson:** Always check group ownership matches the writing user's group, not just the directory owner.

## M26 — SharePoint group IDs mapped to wrong site names

**Symptom:** Config called `ac001f4d` "qnoe-main" and `26a94606` "qnoe-second" — arbitrary names assigned without checking actual group display names.
**Root cause:** IT provided group IDs without labels. We assumed the first was the main QNOE group.
**Reality:** `ac001f4d` = Twisted Materials; `26a94606` = NOE-Group (the one the user actually uses in Teams).
**Fix:** Query `GET /v1.0/groups/{group_id}?$select=displayName` and `GET /v1.0/groups/{group_id}/sites/root?$select=displayName,webUrl` to confirm names before writing config.
**Lesson:** Always resolve group ID → display name before committing to config. Never trust arbitrary naming.

## M27 — Old sync indexed wrong folders before config was corrected

**Symptom:** Proteox, Optical elements, OneNote Uploads indexed into Qdrant from the aborted first run (no exclusions in config).
**Root cause:** Config had `exclude_folders: []` during the first sync run. Corrected config was deployed mid-run.
**Fix:** Deleted sp_manifest DB (`rm -f /opt/qnoe-agent/memory/sharepoint.db`) and purged orphaned Qdrant points via filter delete: `POST /collections/group-wide/points/delete` with filter `repo = "qnoe-main"`. Old site name (`qnoe-main`) distinguished old points from new (`twisted-materials`).
**Lesson:** When aborting a sync mid-run and changing scope, delete sp_manifest and purge Qdrant by `repo` field before restarting.

## M28 — EnvironmentFile not read because file owned by wrong user

**Symptom:** `SharePointPoller: auth failed: Missing credentials` even though `/opt/qnoe-agent/secrets/sharepoint.env` existed.
**Root cause (1):** File created with `sudo tee` (not in NOPASSWD list) silently failed — file was never written.
**Root cause (2):** File written with `chown qnoe-ai:qnoe-ai` + `chmod 600`. Service runs as `yzamir` (not qnoe-ai). `yzamir` is IN the `qnoe-ai` group but had no group-read access.
**Fix:** `sudo chmod 640` so group members (including yzamir) can read it.
**Secondary issue:** Process env checked was from the OLD PID (before file was updated). Needed to restart the service after fixing the file.
**Lesson:** `EnvironmentFile` is read by systemd as root at service start, but the file must be readable. After any secrets file change, always restart the service.

## M29 — MSAL access tokens expire mid-sync (no refresh)

**Symptom (potential):** Graph API 401 errors partway through a long `full_sync()` run.
**Root cause:** `authenticate()` is called once before `full_sync()`. MSAL ROPC tokens expire after 60 min. Large SharePoint drives (NOE-Group: 2h+ sync) exceed this.
**Fix:** Added `_fresh_token()` helper called per-item in both `full_sync()` and `delta_sync()`. Refreshes when `time.monotonic()` elapsed > 45 min. On refresh failure, logs warning and continues with old token.
**File:** `agent/ingest/sharepoint_sync.py`

## M30 — `update_collection` cannot add new vector fields to existing collections

**Symptom:** `update_collection(sparse_vectors_config={"text-sparse": SparseVectorParams(...)})` returns HTTP 400: `"Wrong input: Not existing vector name error: text-sparse"`.
**Root cause:** Qdrant's PATCH `/collections/{name}` endpoint updates existing config (optimizers, HNSW, quantization) but does NOT add new vector fields. The error message is misleading — it's not saying text-sparse already exists; it's saying the PATCH operation doesn't know how to add a new sparse field.
**Fix:** Use `client.create_vector_name(collection_name, vector_name, SparseVectorNameConfig(sparse=SparseVectorConfig()))` — this maps to a different Qdrant endpoint that adds a new vector field to an existing collection.
**Note:** `SparseVectorNameConfig` (for `create_vector_name`) wraps a `SparseVectorConfig` — NOT `SparseVectorParams`. The two models have different fields. `SparseVectorParams` is only for `create_collection`'s `sparse_vectors_config` dict.
**Files:** `agent/ingest/run_ingest.py::_add_sparse_to_collection`, `agent/indexing/backfill_sparse.py::_add_sparse_config`
**qdrant-client version:** 1.18.0

## M31 — fastembed model download blocked by HF_HUB_OFFLINE env var

**Symptom (potential):** `fastembed` fails to load `Qdrant/bm25` model at runtime because `HF_HUB_OFFLINE=1` is set in `embed.py` via `os.environ.setdefault`.
**Root cause:** `embed.py` sets `HF_HUB_OFFLINE=1` before any model loads, blocking all huggingface_hub downloads. fastembed 0.8 uses huggingface_hub for model downloads. If the model isn't cached yet, it fails silently or raises.
**Fix:** Pre-download the model manually BEFORE the offline env var takes effect:
```bash
/opt/qnoe-agent/venv/bin/python3 -c "from fastembed import SparseTextEmbedding; SparseTextEmbedding(model_name='Qdrant/bm25')"
/opt/qnoe-agent/hermes-venv/bin/python3 -c "from fastembed import SparseTextEmbedding; SparseTextEmbedding(model_name='Qdrant/bm25')"
```
**Cache location:** `~/.cache/fastembed/` (18 files, ~1MB total). Once cached, no internet access needed.
**Lesson:** When adding a new fastembed model, always pre-download it on the DGX before deploying code that imports it.

## M32 — Empty files and Git LFS pointers warned every nightly run

**Symptom:** Nightly report repeatedly logs "Could not open PPTX … Package not found" and similar errors for the same files across multiple runs.
**Root cause:** When `chunk_file` returns empty (0-byte file or Git LFS pointer), `_record_file` is never called, so the file never enters the manifest. Every subsequent run re-attempts it from scratch (hash check finds no entry → tries again → fails → no record → repeat).
**Two specific cases found (2026-07-08):**
- `photocurrent-highbias/Subgroups/.../PPT.pptx` — 0-byte git placeholder files
- `Polaritons-On_Chip_FTIR/data/*/B*_Echar*.pptx` — 133-byte Git LFS pointer stubs (real file on LFS server, not pulled locally)
**Fix:** Early detection in `ingest_directory` before `chunk_file` is called:
1. `fsize == 0` → log INFO, `_record_file(... point_ids=[])`, skip
2. `fsize < 200` AND DOCLING extension AND first 40 bytes start with `"version https://git-lfs"` → same
Files are recorded in the manifest with empty `point_ids`, so they're skipped on all future runs until the file actually changes on disk (new hash).
**File:** `agent/ingest/run_ingest.py` — inside `ingest_directory` loop, after hash check.

## M33 — tool_use_enforcement not applied to sub-profiles

**Symptom:** QTM and Photocurrent profiles output tool calls as text (same as M23) even though orchestrator was fixed.
**Root cause:** The M23 fix (`tool_use_enforcement: true`) was only applied to the orchestrator config on DGX. QTM and Photocurrent per-profile `config.yaml` still had `auto`. When users are routed to sub-profiles via `user_profiles.yaml`, they get the unfixed config.
**Fix:** Set `tool_use_enforcement: true` in ALL per-profile configs (QTM + Photocurrent + orchestrator). Also commit per-profile configs to repo (they didn't exist in repo before this audit).
**Lesson:** When fixing a config setting, apply it to ALL profiles — not just the one you tested on. Per-profile configs override the shared config.yaml.

## M34 — TOP_K regressed from 3 to 5 during BM25 deployment

**Symptom:** RAG injecting ~1,200 extra tokens per turn after BM25 hybrid search deployment.
**Root cause:** The I1 context bloat fix changed `TOP_K=5` to `TOP_K=3` directly on DGX but never committed it to the repo. When the BM25 update was deployed from the repo to DGX, it overwrote the fix.
**Fix:** Set `TOP_K = 3` in repo and re-deploy.
**Lesson:** ALWAYS commit config/code changes to the repo immediately after deploying to DGX. DGX-only changes will be lost on next deploy.

## M25 — Qdrant snapshot timestamp lacks timezone

**Symptom:** `TypeError: can't compare offset-naive and offset-aware datetimes` in `task_qdrant_snapshot`.
**Root cause:** Qdrant returns `creation_time` without timezone suffix (e.g., `2026-07-03T08:24:04`). Code does `.replace("Z", "+00:00")` but there's no `Z` to replace. `fromisoformat` returns naive datetime, compared against tz-aware `cutoff`.
**Fix:** Added `if created.tzinfo is None: created = created.replace(tzinfo=timezone.utc)` after parsing.
**File:** `/opt/qnoe-agent/agent/indexing/nightly_run.py` line ~80.

## M35 — Provence reranker is too slow on the Spark CPU (32× cross-encoder)

**Context:** Evaluated `naver/provence-reranker-debertav3-v1` (0.4B DeBERTa-v3, prune+rerank in one model) as a drop-in replacement for the cross-encoder reranker in `qnoe_rag`, to cut RAG injection tokens.
**Finding:** Excellent quality (72% top-3 token reduction, 20/20 answer-keyword survival on 20 QNOE queries), but **CPU latency ~22s/query = 32.5× the cross-encoder's 0.67s**. The reranker runs on CPU because the GPU is fully occupied by vLLM. A 0.4B model doing ~20 forward passes/query (contexts split at Provence's 512-token limit) is inherently slow on the Spark's CPU.
**Why it matters:** Beyond failing the ≤2× latency gate, `qnoe_rag`'s prefetch does `prefetch_thread.join(timeout=10)` — a 22s rerank would time out and return **empty RAG context every turn**, silently breaking retrieval.
**Decision:** NOT deployed. `qnoe_rag` stays on `cross-encoder-msmarco`. Full eval in `logs/provence_eval.md`.
**Lesson:** Any GPU-class reranker/compressor is a non-starter while the GPU is monopolized by a dense 70B on CPU-only inference for aux models. RAG token compression on this box needs either a genuinely tiny CPU model or the MoE model swap (frees GPU headroom). LLMLingua-2 is the noted fallback but is a fresh user decision.

## M36 — Agent/vLLM logs go only to journald, which `yzamir` cannot read

**Symptom:** Can't inspect vLLM startup KV-cache lines or the Hermes gateway's per-turn prompt/token/tool logs; `journalctl -u <svc>` shows "No entries" for `yzamir` (not in `adm`/`systemd-journal`), and `journalctl` is not in the NOPASSWD sudo list.
**Workaround:** Redirect the service's stdout to a file in `scripts/start_vllm.sh` (`... > /opt/qnoe-agent/logs/vllm.log 2>&1`) — the service runs as `qnoe-ai` which can write `logs/`. For offline tool-schema/token measurement, call `model_tools.get_tool_definitions(enabled_toolsets=...)` directly from the venv and tokenize via vLLM's `/tokenize` endpoint (needs no profile access).
**Related limitation:** `hermes prompt-size` (the proper floor tool) needs read access to the 0700/qnoe-ai profile dir; `sudo -u qnoe-ai` is not in NOPASSWD and a temp copy breaks on the profile's cyclic `plugins`/`.env` symlinks — so the fresh-session floor had to be *derived* from the measured tool-schema delta, not read directly.
