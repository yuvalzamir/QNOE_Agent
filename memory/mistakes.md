# Mistakes & Pitfalls
*Last updated: 2026-07-13 (M47 root-cause fixes: forkserver chunking + retry queue)*

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

## M25 — Qdrant snapshot timestamp lacks timezone

**Symptom:** `TypeError: can't compare offset-naive and offset-aware datetimes` in `task_qdrant_snapshot`.
**Root cause:** Qdrant returns `creation_time` without timezone suffix (e.g., `2026-07-03T08:24:04`). Code does `.replace("Z", "+00:00")` but there's no `Z` to replace. `fromisoformat` returns naive datetime, compared against tz-aware `cutoff`.
**Fix:** Added `if created.tzinfo is None: created = created.replace(tzinfo=timezone.utc)` after parsing.
**File:** `/opt/qnoe-agent/agent/indexing/nightly_run.py` line ~80.

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

## M37 — Gateway ignores `toolsets:` config; uses `platform_toolsets` (2026-07-10)

**Symptom:** After toolset slimming (`toolsets: [file, terminal, clarify, qnoe-lab]`), live Teams sessions still had 13 visible tools incl. `skill_manage`/`memory` — the agent listed them when asked.
**Cause:** `gateway/run.py` resolves session toolsets via `_get_platform_tools(user_config, platform_key)`, which reads the **`platform_toolsets:`** config key (per platform, e.g. `teams_polling`) and falls back to subset-inference over the platform's default composite toolset. The top-level `toolsets:` key only affects non-gateway paths. Offline verification via `get_tool_definitions(enabled_toolsets=…)` therefore did NOT match live behavior.
**Fix:** add to shared + all profile configs:
```yaml
platform_toolsets:
  teams_polling: [file, terminal, clarify, qnoe-lab]
```
**Verify live, not offline:** after restart, check `tools.tool_search` log line "N core/visible tools kept" in the profile's `agent.log` on the next session.

## M38 — RAG-only answers confabulate specific QCoDeS runs (2026-07-10)

**Symptom:** Asked "what parameters were recorded in QCoDeS run 75000?", the agent gave a detailed, plausible answer (experiment name, sample, params, timestamp). **Run 75000 does not exist** — max `run_id` in the registry is 59,477. No QCoDeS tool call was made; the model stitched details from semantically-similar RAG chunks.
**Lesson:** existence questions can't be answered from similarity search. For run-id lookups the agent must call the QCoDeS tools (via the tool_search bridge) and report "not found" honestly. Candidate fix: SOUL instruction + re-test (open item).

## M39 — gpt-oss-120b overcommits the 128 GB unified box; vLLM util is measured vs TOTAL device memory, not free RAM (2026-07-10)

**Context:** gpt-oss-120b pilot (see `GPT_OSS_PILOT_PLAN.md`). Model = MXFP4 MoE, **60.8 GiB weights**. Box = single GB10, **128 GB unified** memory shared by GPU+CPU, with Qdrant (~8 GB) + OS/services (~8 GB) already resident.
**Symptom:** vLLM loaded all weights, then during post-load KV-pool allocation / CUDA-graph capture the box drove to **0 available RAM**, fell into full swap (15 GB), and **thrashed into unresponsiveness** — `ssh` failed with *"Connection timed out during banner exchange"* (sshd alive but starved; can't get a scheduling window). Persisted ~40–50 min; only recovered when the OOM-killer finally reaped vLLM.
**Root cause:** On unified memory, vLLM's `--gpu-memory-utilization` (default ~0.9) budgets against **total device memory (128 GB)**, but it does NOT subtract RAM already used by Qdrant + OS (which live in the *same* unified pool). So default util tried to reserve ~115 GB (61 weights + ~54 KV) on top of ~16 GB already used → **~131 GB > 128 GB physical → OOM/thrash.**
**Second trigger:** CUDA-graph capture (80 batch sizes up to 1024) adds a large transient memory peak on top of weights+KV. First attempt OOM-killed during capture. `--enforce-eager` removes that peak but did NOT prevent the KV-alloc overcommit at util 0.78. A confound: attempt-2 was launched **before memory from attempt-1's crash had fully settled** (`Available RAM: 44 GiB` at load), guaranteeing overcommit.
**What would fit (untested, for a supervised retry only):** `--gpu-memory-utilization 0.55`–`0.60` (budget ~70–77 GB = 61 weights + ~9–16 GB KV), `--enforce-eager`, `--max-model-len 65536` (not 131072), `--max-num-seqs 2`, and **launch only when `free -g` shows ≥110 GB available** (wait for the previous process's memory to reclaim). Net system use ~93 GB < 128 → safe. KV pool is then small (~9–16 GB) → limited concurrency/context.
**Recovery lesson:** A GB10 in full-swap-death does not reliably yield an `ssh` banner-exchange window; repeated *single* connection attempts eventually land only once the OOM-killer frees the largest RSS (vLLM). Do NOT connection-storm a starved shared box (auto-mode blocks it). The clean recovery is a **hard reboot** if enabled services auto-start, or a single-connection loop that `pkill`s vLLM then `systemctl start vllm.service`.
**Prevention:** never launch a second large model without first confirming `free -g` available ≥ weights+headroom; treat `gpu-memory-utilization` on unified memory as a fraction of *total* that must also leave room for all non-vLLM residents.
**Outcome:** gpt-oss-120b NOT viable as a drop-in on this single box at target context. Production stays on Hermes-3-70B AWQ. Weights kept on disk at `/opt/qnoe-agent/models/gpt-oss-120b`.

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

## M43 — systemd service crash-loops in 2ms because its log file is owned by the wrong user (2026-07-10)

**Symptom:** during the gpt-oss cutover, `vllm.service` (which now runs `start_llamacpp.sh` as `qnoe-ai`) showed `activating (auto-restart)`, `status=1/FAILURE`, `CPU: 2ms` — dying instantly, before loading any model. `/v1/models` returned nothing, no `llama-server` process. The log file `logs/llamacpp.log` still showed a **stale** boot (frozen, not truncated).
**Cause:** the launch script redirects stdout with `> /opt/qnoe-agent/logs/llamacpp.log`. That file had been created by a **manual test boot run as `yzamir`** (owner `yzamir:Domain Users`, mode 644). When systemd ran the script as `qnoe-ai`, the shell could not open the file for writing (truncate) → script exits 1 before the `exec` → restart loop. The 2ms CPU + un-truncated stale log are the tell.
**Fix:** `sudo chown qnoe-ai:qnoe-ai /opt/qnoe-agent/logs/llamacpp.log` (or delete it so the service creates it fresh), then `sudo systemctl restart`.
**Lesson:** any file a manual test boot writes into a service-owned dir (esp. the redirect target log) must be chowned back to the service user before systemd takes over — a service-user process cannot truncate a file owned by another user even in a group-writable dir if the file itself isn't group-writable. When a service dies with ~2ms CPU and a frozen log, suspect the log redirect, not the model.

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


## M46 — Mem0 memory poisoning: the agent's confabulations became "remembered facts" (2026-07-10)

**Symptom:** after one fabricated superconductivity survey (rag_chars=0 → prior-knowledge answer with false lab attribution), the SAME wrong content came back in the next fresh session — cited as "(Source: persistent memory context)". Injection log: `mem_facts=3 rag_chars=0`; Qdrant scroll found 3 poisoned facts distilled from the bad answer ("The QNOE Superconductivity sub-team studies… hydrides…").
**Mechanism:** `sync_turn()` fed BOTH user and assistant messages to `mem0.add()` — Mem0 distilled the assistant's claims into user-keyed facts. Confabulate once → remember forever → self-reinforcing.
**Fixes (all 2026-07-10):** (1) purge poisoned points (Qdrant delete by id); (2) `mem0.add()` now receives the **user message only**; (3) SOUL rule: the memory block is about the USER — never a source for physics/lab facts.
**Lessons:** (1) any write-back memory over agent output is a confabulation amplifier — store only user-authored content unless outputs are verified; (2) the per-turn injection log (M45's fix) is what made this diagnosable in one look; (3) test memory with a QUESTION THE AGENT PREVIOUSLY ANSWERED WRONGLY — that's the poisoning probe.


## M47 — SharePoint poller: silently dropped files, invisible to the nightly report (2026-07-13)

**Symptom:** user added ~13 papers to `TwistedMaterials/QTOM/Relevant papers` on 2026-07-10; 11 got indexed, **2 never did** and appeared in **no** nightly report (07-10, 07-11, or later). Files: `proposed-quantum-twisting-...pdf`, `revealing-electron-electron-interactions-...pdf`.

**Three stacked bugs found:**
1. **ProcessPoolExecutor worker crash on the 2nd Docling conversion.** `_chunk_file_safe` uses a fresh `_PPE(max_workers=1)` per file, but back-to-back conversions in one process crash the forked worker (`_BrokenExecutor`). File #2 failed via the pool yet chunked fine (27 chunks/8s) when run standalone — proving it's a flaky pool crash, not a bad PDF. First-in-process item succeeds; later ones are at risk.
2. **`delta_sync` advances the Graph delta token unconditionally** (`_save_delta_link` at end of each drive pass) even when items errored/skipped. A once-failed file is **never retried** — the delta only re-surfaces it if the file itself changes. Silent permanent loss.
3. **Failures were counted as invisible "skips."** A chunk crash makes `_process_item` return `False` → tallied as `skipped`, NOT `errors`/`failed_files`. The report line only rendered `✓`/`✗`; skips (and their filenames) vanished. Worse — the **30-min watcher `SharePointPoller` does the real ingestion**, but its stats go only to journald; the nightly `task_sync_sharepoint` re-runs `delta_sync` and sees ~0 because the poller already consumed the token. So poller work (success OR failure) never reached any report.

**Fixes (2026-07-13, deployed + verified):**
- **Backfilled both files** (19 + 27 chunks) by running `_process_item` one-file-per-fresh-process (avoids bug #1).
- **Reporting (the user's ask):** new `sp_activity` table in `sharepoint.db`; `record_sp_activity(source, site, stats)` called by the poller (`source="poller"`) and nightly (`"nightly"`); `summarize_sp_activity(24)` aggregated into `task_sync_sharepoint` stats as `poller_activity_24h`; both txt (`_summarise_stats`) and Teams (`_task_detail`) renderers now show a `poller (24h): …` line **with a `dropped:` list** of skipped/failed filenames. `delta_sync` now records skipped **names**, not just a count. Files touched: `sharepoint_sync.py`, `smb_watcher.py`, `nightly_run.py`, `post_report.py`.

**Root-cause fixes (2026-07-13, branch `feature/sp-ingest-hardening`, deployed + validated):**
- **Bug #1 (chunk crash) → forkserver.** `_chunk_file_safe` now runs Docling in a `multiprocessing.get_context("forkserver")` pool, not a plain fork. The fork crashed because by mid-batch the main process had loaded torch/onnxruntime (threads + CUDA) via `embed_documents`, and forking that state segfaults the worker. forkserver forks each worker from a clean single-threaded server started once (via `_ensure_chunk_server()`, called at the top of delta_sync/full_sync **before** any embedding) — fork-speed, no torch inheritance. **NOT spawn**: spawn re-imports the whole module tree per file (torch/docling), which would cripple full_sync's 76K files. Validated: two PDFs chunk in one process (19 + 27 chunks) *with torch loaded in the main process* — the exact case that deterministically crashed. Requires the entry modules to guard `if __name__ == "__main__"` (all three do).
- **Bug #2 (lost failures) → retry queue.** New `sp_retry_queue` table. `_process_item` now **raises** on retryable failures (chunk crash, download/embed error) instead of returning `False` (permanent skips — unsupported/oversized/excluded/unchanged/empty — still return `False`). `delta_sync` parks raised failures via `_enqueue_retry`, and re-attempts parked items at the top of each cycle via `_process_retry_queue` (re-fetches item metadata from Graph by id; dequeues on success; bounded by `SP_RETRY_MAX_ATTEMPTS=5`, after which items are flagged exhausted and logged). The delta token still advances (no reprocessing the whole change set), but failures are no longer lost.

**Lesson added:** `ProcessPoolExecutor` default fork is unsafe once the parent loads a threaded/CUDA lib (torch). Use **forkserver** (clean server, fork-speed) — not spawn (per-task re-import) — and start the server from a pre-torch state.

**Why it's unique to SharePoint:** SMB changes are *detect-only* → queued to `change_queue` → ingested+reported by the nightly `task_process_change_queue`. The SP poller is the **only** daemon path that performs terminal ingestion itself (the Graph delta token is consumed on read, so it can't defer to a batch) — which is exactly why its work was invisible.

**Lessons:** (1) a "skip" that is actually a failure is worse than an error — it's silent; count/emit skipped filenames. (2) Any daemon that ingests directly (not via a reported queue) needs its own activity log the report reads. (3) `ProcessPoolExecutor` workers can carry corruption between tasks — for crash-prone native libs (Docling), isolate one task per process. (4) A consumed-on-read cursor (Graph delta token) must not be advanced past items that failed to process.
