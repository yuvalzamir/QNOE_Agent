# Infrastructure
*Last updated: 2026-07-14 (B7 systemd sandbox on qnoe-hermes)*

> DGX hardware, services, networking, and system-level config.
> Full setup guide: [[DGX_SETUP]] · Current state: [[SETUP_LOG]] · Deploy procedures: [[memory/deploy-patterns]]

## GitHub Repository

**URL:** https://github.com/yuvalzamir/QNOE_Agent.git
**Branch:** `master`
**Setup:** 2026-07-06 — initial commit of 106 files.

**Excluded from repo:** `secrets/` (Teams credentials), `SSHKey.txt` (DGX private key), `tmp/`, `.claude/settings.local.json`.
**Included:** all docs, memory vault, agent code, Hermes profiles + plugins, configs, scripts, runbooks.

```bash
# Clone
git clone https://github.com/yuvalzamir/QNOE_Agent.git

# Push changes
cd Z:/code/AI_Student && git add -A && git commit -m "..." && git push
```

## SSH Access

```bash
ssh -i "/c/Users/yzamir/.ssh/id_ed25519_dgx" -o StrictHostKeyChecking=no yzamir@10.3.8.21 "command"
```

**NOPASSWD sudo** for: `cp`, `chown`, `chmod`, `mkdir`, `systemctl`, `cat`. Other sudo commands need user to run manually.

## CIFS Mount (Lab Data Server)

```bash
sudo mount -t cifs "//files/groups/NOE" /ICFO/groups/NOE -o username=yzamir,domain=ICFONET
```

Does NOT persist across reboots. Re-run after each restart. Prompts for ICFO password.

## Services

| Service | Status (2026-06-30) | Details |
|---|---|---|
| Inference | systemd `vllm.service` (unit name kept) | localhost:8000, **gpt-oss-120b MXFP4 via llama.cpp** since 2026-07-10 cutover — was Hermes 3 70B AWQ/vLLM. Runs `scripts/start_llamacpp.sh`. |
| Qdrant | Docker container | port 6333, 8 collections, data at `/opt/qnoe-agent/qdrant_data/` |
| Watcher | systemd `qnoe-watcher.service` | SMB3 file watcher, ~37K cached files |
| Hermes Agent | systemd `qnoe-hermes.service` | Native (no Docker), Teams polling, per-user profile routing. **Since 2026-07-14: runs under B7 read-only namespace** (see section below) |

## B7-OS: OpenShell Sandbox on the Hermes Gateway (ACTIVE since 2026-07-14 evening, [[memory/decisions#D18]])

**Production unit: `qnoe-hermes-sandbox.service`** (enabled at boot) → `scripts/start_hermes_sandbox.sh` →
`openshell sandbox create --name qnoe-hermes --from qnoe-hermes:0.1 --policy config/sandbox-policy.yaml
--driver-config-json "$(cat config/hermes-sandbox-mounts.json)" -- scripts/start_hermes.sh`, uid 1001.
`Conflicts=qnoe-hermes.service` both ways = single-Teams-poller guarantee.

- **Stack:** OpenShell v0.0.82 (deb; 0.0.59 deb kept in /tmp for rollback) · `openshell-gateway.service`
  now passes `--config /opt/qnoe-agent/config/gateway.toml` (`enable_bind_mounts = true`) · image
  `qnoe-hermes:0.1` (Dockerfile.hermes; build from an ISOLATED context dir, never /opt/qnoe-agent) ·
  default-deny mounts per `config/hermes-sandbox-mounts.json` · landlock + L7 egress proxy per
  `config/sandbox-policy.yaml` (single source of truth).
- **Verified 2026-07-14:** b7_probe 24/24 in-sandbox (`sudo systemctl start qnoe-b7-sandbox-test`);
  Teams/Mem0(across restarts)/RAG/qcodes-848; R4 perm-write probe = model attempted, EROFS, file
  unchanged; drills: gateway-restart self-heal (~60s), docker kill, clean stop, Conflicts flips.
- **Rollback (any time, seconds):** `sudo systemctl start qnoe-hermes` (systemd-namespace mechanism
  below — kept installed+disabled). Full OpenShell rollback: 0.0.59 deb + revert gateway.toml.
- **Runtime traps + fixes:** [[memory/mistakes#M51]] (HOME=/sandbox, landlock /dev/null + read
  restrictions, L7 proxy-by-hostname via host.openshell.internal, no hardcoded localhost) and
  [[memory/mistakes#M50]] (diff live vs repo before overwriting deployed plugins).
- **Ops notes:** teams.env is bind-mounted by inode — rotate ⇒ `systemctl restart qnoe-hermes-sandbox`.
  Probe/launcher scripts must `openshell sandbox delete` (containers linger after command exit; a
  lingering gateway container = 2nd poller). Audit: `sudo systemctl status openshell-gateway -n 200`
  (L7 denials) + `~qnoe-ai/.local/state/openshell/gateway/openshell.db`. Host /etc/hosts has
  `127.0.0.1 host.openshell.internal` (LLM base_url alias, all 4 hermes configs). Harness Channel A
  (`hermes -z`) remains UNCONFINED on the host.
- **Pending soak:** nightly cron visibility (morning after 2026-07-14), SharePoint-sync query, watch
  `logs/` + journal for landlock/proxy denials in daily use.

## B7 Read-Only Sandbox via systemd namespace (2026-07-14 — SUPERSEDED same day by B7-OS above; unit kept as ROLLBACK, [[memory/decisions#D17]])

Red-team R4 proved read-only was SOUL-only (agent wrote a repo file 1/5 runs). OS-enforced via
systemd mount namespace — drop-in `/etc/systemd/system/qnoe-hermes.service.d/50-b7-readonly.conf`
(repo copy: `hermes/scripts/qnoe-hermes.service.d/`):

- `ReadOnlyPaths=/opt/qnoe-agent /ICFO /mnt/noe /home/yzamir` — binds `write_file`/`patch` and every `terminal` child.
- `ReadWritePaths=/opt/qnoe-agent/memory /opt/qnoe-agent/logs /opt/qnoe-agent/hermes` (Mem0/SQLite, logs, session state). `/home/qnoe-ai` (`.mem0`, `.cache`) stays rw by default.
- `InaccessiblePaths=/opt/qnoe-agent/secrets` + `LoadCredential=teams.env:...` — systemd (root, outside the namespace) delivers teams.env via `$CREDENTIALS_DIRECTORY`; `start_hermes.sh` sources it there, falling back to the direct path for bare/rollback runs.
- `NoNewPrivileges` + `PrivateTmp` + `ProtectSystem=full`.

**Verification:** standing unit `qnoe-b7-test.service` runs `scripts/b7_probe.sh` under the *same*
directives (keep the two units in sync). `sudo systemctl start qnoe-b7-test` → all-PASS lines in
`logs/b7_probe.log`. 2026-07-14: 20/20.

**Gotchas:** (1) red-team harness Channel A (`hermes -z`) runs OUTSIDE the unit → NOT sandboxed;
enforcement checks go via Teams or the probe unit. (2) cron jobs (nightly re-index git-pulls
`repos/`, SharePoint sync) run outside the unit → unaffected, still writable for them. (3) sqlite
registry DBs are `journal_mode=delete`, so ro readers need no side files — do NOT switch them to WAL
without revisiting the ro mounts. (4) Rollback: `sudo cp /dev/null .../50-b7-readonly.conf && sudo
systemctl daemon-reload && sudo systemctl restart qnoe-hermes` (no sudo rm in NOPASSWD set).
Backup of the pre-B7 launcher: `scripts/start_hermes.sh.bak-pre-b7` on the DGX.
(5) **Enforcement is an allowlist of paths — new mounts are writable by default.** Found + fixed
2026-07-14: `/mnt/noe` (second CIFS mount of the SAME NOE share, `uid=1001` = qnoe-ai, used by the
watcher) was rw inside the namespace — a full bypass of the `/ICFO` ro mount. Any NEW mount or
qnoe-ai-writable path added to the host must be added to `ReadOnlyPaths=` in BOTH units, and a
matching check added to `b7_probe.sh`.

## Key Paths on DGX

| Path | Purpose |
|---|---|
| `/opt/qnoe-agent/` | Main install (owned by `qnoe-ai:qnoe-ai`, uid 1001) |
| `/opt/qnoe-agent/venv/` | Agent Python venv |
| `/opt/qnoe-agent/hermes-venv/` | Hermes Agent venv (separate — openai conflict) |
| `/opt/qnoe-agent/hermes/` | Hermes home (`HERMES_HOME` env var) |
| `/opt/qnoe-agent/models/` | LLM + embedding models |
| `/opt/qnoe-agent/repos/` | Cloned GitHub repos (41 repos) |
| `/opt/qnoe-agent/memory/` | Checkpoints, episodic DB |
| `/opt/qnoe-agent/logs/` | All logs |
| `/opt/qnoe-agent/config/` | YAML configs, sandbox policy |
| `/opt/qnoe-agent/secrets/` | GitHub PAT, Teams env, SharePoint credentials |
| `/ICFO/groups/NOE` | Lab data server mount |

## Agent Container

- Image: `qnoe-agent:latest`
- Runs as `--user 1001:1001` (qnoe-ai)
- `--env-file /opt/qnoe-agent/secrets/teams.env`
- Needs `/etc/passwd` entry for uid 1001 (PyTorch requirement) — generated at startup
- Start script: `/opt/qnoe-agent/scripts/start_agent.sh`
- Logs: `/opt/qnoe-agent/logs/agent.log`

## Memory & Capacity

- **Total unified memory:** 121GB (CPU+GPU shared — DGX Spark GB10)
- **At rest (vLLM running):** ~117-120GB used — model weights ~40GB + KV pool ~67GB (fp8) + OS/services
- **At rest (vLLM stopped):** ~5GB used
- **64K context: FEASIBLE (deployed 2026-07-09)** — the old "+40GB not feasible" note conflated *per-sequence* KV with the pre-allocated *pool*. The KV pool size is set by `gpu_memory_utilization`, NOT by `max_model_len`. Raising `max-model-len` to 65536 costs zero extra memory. Measured pool at 64K + `--kv-cache-dtype fp8`: **Available KV cache 67.4GB → ~471K KV tokens → 7.2× concurrency at full 64K** (fp16 would give ~232K tokens / 3.5×). See [[CONTEXT_PRESSURE_REPORT]] §3.
- **Concurrent users:** ≥3 guaranteed at full 64K (fp8 pool = ~471K KV tokens; `--max-num-seqs 4`). Decode stays ~6 tok/s regardless (bandwidth-bound; batching amortizes weight streaming up to ~4 streams). Single "who are you" still ~16-20s.
- **Embedding model memory:** nomic-embed tensors are evicted to swap under memory pressure (e.g. SharePoint digest running). Appears as slow "reload" even though lru_cache holds the object. Fix: dedicated embedding microservice (future work).
- **vLLM startup (current, fp8/64K, 2026-07-09):** `vllm serve /opt/qnoe-agent/models/hermes-3-70b-awq --host 0.0.0.0 --port 8000 --quantization awq_marlin --max-model-len 65536 --kv-cache-dtype fp8 --max-num-seqs 4 --enable-auto-tool-choice --tool-call-parser hermes` (script `scripts/start_vllm.sh` also redirects stdout→`logs/vllm.log` since the service otherwise only logs to journald). fp8 KV benchmarked ≥ fp16 decode (6.11 vs 5.96 tok/s), tool-calling + quality intact.
- **To free memory for large ingestion jobs:** `sudo systemctl stop vllm.service` (frees ~115GB). Restart: `sudo systemctl start vllm.service` (~1-2 min to load with llama.cpp mmap).

## Serving Stack — gpt-oss-120b via llama.cpp (CUTOVER 2026-07-10)

Production inference switched from **Hermes 3 70B AWQ (vLLM)** to **gpt-oss-120b MXFP4 (llama.cpp)**. The systemd unit name `vllm.service` was intentionally **kept** to preserve the `Requires=vllm.service` chain in `qnoe-hermes.service` and every runbook. Hermes-3 stays on disk as the documented fallback.

- **Server:** `/opt/qnoe-agent/llamacpp/bin/llama-server` (+ co-located `.so` libs; launch script sets `LD_LIBRARY_PATH=/opt/qnoe-agent/llamacpp/bin`).
- **Model:** `/opt/qnoe-agent/models/gpt-oss-120b-gguf/` — 3 MXFP4 GGUF shards (~63GB). (The separate `models/gpt-oss-120b/` safetensors dir is an earlier vLLM-path artifact, unused by llama.cpp.)
- **Launch:** `scripts/start_llamacpp.sh` — `-ngl 999 --flash-attn on -ub 2048 -c 262144 --parallel 4 --jinja --chat-template-kwargs '{"reasoning_effort":"low"}' --alias gpt-oss-120b`, stdout→`logs/llamacpp.log`.
- **KV config decision:** `-c 262144 --parallel 4` **WITHOUT** `--kv-unified` → **4 fixed slots × 65536 (64K) each = 262144-token KV pool**, guaranteeing ≥4 concurrent users at full 64K. `--kv-unified` (community default) was dropped **on purpose**: with it, per-slot ctx is set to the full `-c` then capped to the model's 131072 train context, yielding a *shared* 128K pool (only ~32K/user at 4 concurrent). Non-unified delivers the required 4×64K. Verified in log: `n_slots=4, n_ctx_slot=65536, kv_unified='false'`.
- **Measured (2026-07-10):** ~63GB weights (mmap) + KV/compute → **44-48GB RAM available while serving** (>> 20GB floor). Decode **46.6 tok/s** single-stream (8× the old Hermes-3 ~6 tok/s), **3 concurrent streams @ 25.5 tok/s each** (~76 tok/s aggregate). TTFT ~0.13s warm (prefix cache), ~3.7s cold on 10K prompt.
- **`reasoning_effort:low` baked in** at server level via `--chat-template-kwargs` — mitigates gpt-oss's empty-content trait (reasoning eating the output budget). Simple Q with `max_tokens:400` returns non-empty content. NOTE: with **no tools provided**, a "look this up" question can still yield empty content (all tokens go to `reasoning_content` deciding to search) — in production the agent provides tools so this becomes a tool call.
- **Structured tool calls:** verified at 151 / 8181 / 16210 / 32305 prompt tokens — no cliff (retires the old "19.5K cliff", see [[memory/mistakes#M40]]).
- **Rollback:** point `vllm.service` `ExecStart=` back to `scripts/start_vllm.sh` (untouched, still the fp8/64K Hermes-3 config), `daemon-reload`, revert the 3 profile configs + `MEM0_LLM_MODEL`, restart. See [[memory/decisions#D14]].
- **PITFALL hit during cutover:** the systemd service (runs as `qnoe-ai`) crash-looped `status=1` in 2ms because `logs/llamacpp.log` was owned by `yzamir` (from a manual test boot) and `qnoe-ai` could not truncate it for the `>` redirect. Fix: `sudo chown qnoe-ai:qnoe-ai logs/llamacpp.log`. Any file a test boot creates in `logs/` must be chowned back before systemd owns the process.

## SharePoint Integration

Added 2026-07-03. Two Teams-connected sites ingested via Microsoft Graph API (delta sync).

**App registration:** `108a03c5-e265-4ab6-a5ea-9c902fd527d4`, tenant `f78a768a-22ae-4432-9eb4-55ce4b73c8c3`
**Auth:** ROPC (username/password) via MSAL. Credentials in `/opt/qnoe-agent/secrets/sharepoint.env` (mode 640, owner qnoe-ai).
**Config:** `/opt/qnoe-agent/config/sharepoint.yaml`
**SP manifest DB:** `/opt/qnoe-agent/memory/sharepoint.db` (etag-based dedup, separate from repo manifest)
**Delta links:** stored in `sharepoint_delta` table in watcher DB (`/opt/qnoe-agent/memory/watcher.db`)

| Site name in config | Teams group | SharePoint URL | What's indexed |
|---|---|---|---|
| `twisted-materials` | Twisted Materials - shared equipment | `icfo.sharepoint.com/sites/TwistedMaterials-sharedequipmentandexperiments` | QTOM, SpectroMag, THz gas laser only |
| `noe-group` | NOE-Group | `icfo.sharepoint.com/sites/NOE-Group` | Everything |

**Excluded from twisted-materials:** General, OneNote Uploads, Optical elements, Proteox, Quotes

**Drive IDs (confirmed 2026-07-03):**
- twisted-materials/Documents: `b!2htNylQI70ynH2cuAE5BSBP3-kmWS2tKmGB-0K4eNTDyWwfMXVEgTah62t3q-B7w`
- noe-group/Documents: `b!6T8n2h74TUuwrCDNas_S6aIAyKOIvEJCshcZSSKGoTlNllfVKVqKSLJiIV06jfMU`

**Token refresh:** auto-refreshes every 45 min during long syncs (tokens expire at 60 min).
**Temp files:** written to `/tmp/qnoe-sharepoint/{site_name}/`, deleted immediately after chunking.
**Poll:** `SharePointPoller` thread in watcher daemon, every 30 min via delta API.
**Nightly:** `task_sync_sharepoint()` runs full sync as safety net (after `task_index_repos`).

**Systemd service** (`qnoe-watcher.service`) updated to load `EnvironmentFile=-/opt/qnoe-agent/secrets/sharepoint.env` and `Environment=SHAREPOINT_CONFIG=/opt/qnoe-agent/config/sharepoint.yaml`.

**Validate access:**
```bash
cd /opt/qnoe-agent && SHAREPOINT_USERNAME=... SHAREPOINT_PASSWORD=... python -m agent.ingest.sharepoint_sync --validate
```

## Nightly Cron

```bash
0 2 * * * PYTHONPATH=/opt/qnoe-agent QDRANT_URL=http://localhost:6333 REPOS_DIR=/opt/qnoe-agent/repos AGENT_DATA_DIR=/home/yzamir/qnoe_server_data SERVER_ROOT=/ICFO/groups/NOE COLLECTIONS_CONFIG=/opt/qnoe-agent/config/repo_collections.yaml /opt/qnoe-agent/venv/bin/python -m agent.indexing.nightly_run >> /opt/qnoe-agent/logs/nightly_reindex.log 2>&1
```

4 tasks: Qdrant snapshots, repo re-index, change queue processing, orphan cleanup. See [[memory/ingestion]] for details.
