# Mistakes & Pitfalls
*Last updated: 2026-07-08 (M33-M34 added — migration audit findings)*

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

## M40 — The "~19.5K tool-calling cliff" was prose-fallback, not a model limit (2026-07-10)

**What we believed (D11 era):** Hermes-3 loses structured `tool_calls` past ~19.5K prompt tokens (worked at 359, failed at ~19.5K live) — treated as a hard model ceiling; sized the whole context discipline around it.
**What the gpt-oss pilot measured:** bare probes (same model, same vLLM, same hermes parser; neutral filler + small tool list + clear instruction) returned structured tool calls at **400 / 8.3K / 16.4K / 32.4K tokens**. No cliff in our operating range.
**Actual mechanism — prose-fallback:** in agent-shaped context (many tool schemas, RAG chunks, multi-turn prose, long tool outputs) the model writes the call as prose (`read_file(path="…")`) instead of the structured channel; the parser then yields nothing. Failure tracks context *composition*, not length (matches IBM LongFuncEval: degradation scales with tool-catalog size and tool-output length).
**Lessons:**
1. Retire "19.5K" as a constant — watch for prose-fallback *symptoms* (tool syntax appearing in reply text), not a token number.
2. Tool-schema slimming (12→7 resident) attacks the mechanism, not just the budget.
3. Deterministic context hooks (QCoDeS registry lookup) bypass the model's tool decision entirely — immune to this failure class; prefer them for must-not-fail lookups.
4. `tool_use_enforcement: true` stays as a guard while on Hermes-3.

Closes roadmap step 5 of [[CONTEXT_PRESSURE_REPORT]]. Numbering note: M39 (unified-memory overcommit) lives on branch `feature/gpt-oss-pilot`.

## M41 — venv vLLM cannot boot gpt-oss-120b on the Spark: Marlin repack doubles weight memory (2026-07-10)

**Symptom:** supervised retry on an idle box (117 GB available, `--gpu-memory-utilization 0.55 --max-model-len 32768 --enforce-eager --max-num-seqs 2`): all 15 shards loaded (61 GB, ~7.5 min), then available RAM collapsed 45 GB → **0** within seconds of `Using MoEPrepareAndFinalizeNoDPEPModular` (Marlin post-load init).
**Cause:** the Marlin MXFP4 path transiently needs a ~second copy of the weights during repacking → ~120+ GB peak > 128 GB unified. It is an **init-phase peak** — no flag tuning can fix it on this build (vLLM 0.22.1 venv).
**Containment that worked:** a 5-second memory watchdog (`pkill -9` the pilot when available < 10 GB) turned the previous 40-50 min box hang into a ~35 min recoverable window; box needed no manual intervention. Pattern: ALWAYS run the watchdog before any experimental model boot.
**Remaining candidates for gpt-oss-120b on one box:** NVIDIA vLLM container (possible in-place load), llama.cpp GGUF (mmap, no repack spike — source of the community 45-59 tok/s numbers), or drop to a smaller MoE (Qwen3-class A3B). Recovery pattern for a thrashing box: per-phase retrying SSH loop (cleanup → start vllm → health poll → restart hermes).

## M42 — `pkill -f` over SSH kills your own remote shell (self-match) (2026-07-10)

**Symptom:** every `ssh dgx "pkill -9 -f llama-server; …"` exited 255 with no output; three "successful-looking" production restores later, port 8000 was STILL serving the pilot llama-server — and `vllm.service` showed `active`/`activating` while endlessly failing to bind the port. Net effect: the lab agent unknowingly served gpt-oss for ~20 min.
**Cause:** `pkill -f PATTERN` matches full command lines — including the remote bash running the compound command, whose cmdline contains the pattern string. The shell kills itself (exit 255, no output) and the intended target can survive. `2>/dev/null` + retry loops made it look like transient SSH flakiness.
**Fix:** self-safe patterns: `pkill -9 -f 'llama[-]server'` (bracket breaks self-match) or `pkill -x <comm>`. **Always verify the kill**: `pgrep -cf 'llama[-]server' || echo ZERO` — and verify what is actually serving the port (`curl /v1/models`), not just `systemctl is-active` (a service can be "active" while crash-looping on a taken port).

## M44 — Production agent couldn't read the lab-server QCoDeS registry (700 home dir) (2026-07-10)

**Symptom:** live run-159 answer said "35 databases" (SharePoint entries only); true total is 49. All sample rows were `/tmp/qnoe-sharepoint-qcodes/` paths.
**Cause:** `AGENT_DATA_DIR=/home/yzamir/qnoe_server_data`, but `/home/yzamir` was mode 700 — `qnoe-ai` (the service user) can't traverse, so `os.path.exists()` is False and the registry hook silently skips it. **The qnoe_qcodes plugin tools use the same path — production had likely NEVER seen the 75,994 lab-server runs, only SP-ingested ones.** Caught only because the hook reports a total count (the count-honesty fix from earlier the same day).
**Fix:** `sudo chmod o+x /home/yzamir` (traverse-only; home stays unlistable/unreadable — `qnoe_server_data` itself was already 755). Verify by re-asking run 159 → expect 49.
**Lessons:** (1) test data-access paths AS THE SERVICE USER, not as yourself; (2) deterministic counts in injected context act as integrity checks — a wrong count exposed this; (3) long-term: move shared data out of a user home (Phase-2 item).

## M45 — Mem0 recall silently broken: Hermes `prefetch_all()` passes no session_id → uid "anon" (2026-07-10)

**Symptom:** stated preferences landed in Qdrant under the correct Teams user_id, but the agent NEVER recalled them in later sessions ("I don't have any record…"), even though offline `mem0.search()` ranked the fact #1 for the exact question.
**Diagnosis:** the per-turn injection log (added same day) showed `mem_facts=0 … session=''` — Hermes core (`turn_context.py:392`) calls `memory_manager.prefetch_all(_query)` WITHOUT `session_id`, so the plugin's `_uid_for("")` fell through to uid **"anon"** → empty search. The write path (`sync_all`) DOES pass session_id, which is why storage looked healthy. The original "verification" passed only because the fact was still inside the conversation window.
**Fix (plugin-side, survives Hermes upgrades):** remember `self._last_uid` in `initialize()` (which gets session+user every turn) and fall back to it when `session_id` is empty. Caveat: truly concurrent multi-user turns could briefly attribute a read to the wrong user — read-side only, acceptable; revisit if Hermes core ever passes session_id (candidate upstream one-liner).
**Lessons:** (1) verify memory recall in a FRESH session, not the session where the fact was stated; (2) per-turn injection logging (mem_facts / qcodes_block / rag_chars / session) is what made this diagnosable in one look — keep it.
