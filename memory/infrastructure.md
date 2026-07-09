# Infrastructure
*Last updated: 2026-07-06 (GitHub repo added)*

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
| vLLM | systemd `vllm.service` | localhost:8000, Hermes 3 70B AWQ, awq_marlin, 32K context |
| Qdrant | Docker container | port 6333, 8 collections, data at `/opt/qnoe-agent/qdrant_data/` |
| Watcher | systemd `qnoe-watcher.service` | SMB3 file watcher, ~37K cached files |
| Hermes Agent | systemd `qnoe-hermes.service` | Native (no Docker), Teams polling, per-user profile routing |

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
- **To free memory for large ingestion jobs:** `sudo systemctl stop vllm.service` (frees ~115GB). Restart: `sudo systemctl start vllm.service` (~5 min to load).

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
