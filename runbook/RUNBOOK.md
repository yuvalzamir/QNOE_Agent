# QNOE Lab Agent — Setup Runbook

> **Audience:** Someone setting this up from scratch on a new DGX Spark (or similar Ubuntu aarch64 server).
> **Target machine:** DGX Spark, Ubuntu 24.04, 128 GB unified memory, NVIDIA GB10 GPU.
> **Goal:** A fully local AI agent serving the QNOE group via Microsoft Teams.

---

## Overview

The system has four main components:

| Component | What it does | Where it runs |
|---|---|---|
| **vLLM** | Serves Hermes 3 70B AWQ — the main language model | systemd service |
| **Qdrant** | Vector database — stores all indexed knowledge | Docker container |
| **OpenShell gateway** | Sandboxes the agent in a Docker container with policy enforcement | systemd service |
| **Agent** | Python app — reads Teams messages, queries Qdrant, calls vLLM, replies | systemd service (disabled until wired to Teams) |

Everything lives under `/opt/qnoe-agent/` owned by the `qnoe-ai` service account.

---

## Prerequisites

- Ubuntu 24.04 on aarch64 (NVIDIA DGX Spark or equivalent)
- `yzamir` user exists with `sudo` access
- `qnoe-ai` service account exists (ask IT to create it)
- `python3.12-dev` installed: `sudo apt install python3.12-dev`
- `uv` installed for `yzamir`: `curl -LsSf https://astral.sh/uv/install.sh | sh`
- Docker installed and `yzamir` + `qnoe-ai` in the `docker` group
- NVIDIA container runtime configured

---

## Step 1 — Directory structure

```bash
sudo mkdir -p /opt/qnoe-agent/{models,venv,qdrant_data,memory,secrets,logs,config,scripts,repos,agent}
sudo chown -R qnoe-ai:qnoe-ai /opt/qnoe-agent
sudo chmod g+w /opt/qnoe-agent/memory   # yzamir writes server ingest manifest here
sudo usermod -aG qnoe-ai yzamir          # so yzamir can write to memory/
```

---

## Step 2 — Python venv

```bash
sudo -u qnoe-ai uv venv /opt/qnoe-agent/venv --python 3.12
```

Install all dependencies:

```bash
cd /tmp && sudo -u qnoe-ai HOME=/home/qnoe-ai uv pip install \
  --python /opt/qnoe-agent/venv/bin/python \
  vllm \
  sentence-transformers \
  qdrant-client \
  langgraph \
  langgraph-checkpoint-sqlite \
  openai \
  msal \
  python-pptx \
  python-docx \
  docling \
  pypdf \
  pyyaml \
  gitpython
```

---

## Step 3 — Download models

**Hermes 3 70B AWQ** (~40 GB):
```bash
sudo -u qnoe-ai HOME=/home/qnoe-ai /opt/qnoe-agent/venv/bin/huggingface-cli download \
  mbley/NousResearch-Hermes-3-Llama-3.1-70B-AWQ \
  --local-dir /opt/qnoe-agent/models/hermes-3-70b-awq
```

**nomic-embed-text-v1.5** (~2 GB):
```bash
sudo -u qnoe-ai HOME=/home/qnoe-ai /opt/qnoe-agent/venv/bin/huggingface-cli download \
  nomic-ai/nomic-embed-text-v1.5 \
  --local-dir /opt/qnoe-agent/models/nomic-embed
```

> **Note:** Docling also downloads layout models on first use (~1 GB). These cache to `/home/qnoe-ai/.cache/docling/` automatically on first PDF ingestion.

---

## Step 4 — Qdrant (Docker)

```bash
sudo docker run -d \
  --name qdrant \
  --restart unless-stopped \
  -p 6333:6333 \
  -v /opt/qnoe-agent/qdrant_data:/qdrant/storage \
  qdrant/qdrant:latest
```

Verify:
```bash
curl http://localhost:6333/healthz   # → {"title":"qdrant","version":"..."}
```

Create the 7 knowledge collections (768-dim cosine):
```bash
for col in qtm photocurrent qed superconductivity qsim xchiral group-wide; do
  curl -s -X PUT http://localhost:6333/collections/$col \
    -H "Content-Type: application/json" \
    -d '{"vectors":{"size":768,"distance":"Cosine"}}'
done
```

---

## Step 5 — vLLM systemd service

Install the service file:
```bash
sudo cp /opt/qnoe-agent/config/vllm.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now vllm
```

Check it started (takes ~5 minutes on cold start):
```bash
sudo systemctl status vllm
curl http://localhost:8000/health   # → {"status":"ok"}
```

> **Key gotcha:** The service `Environment=PATH=` line must include `/opt/qnoe-agent/venv/bin` — `ninja` (needed by FlashInfer JIT) lives there. See `config/vllm.service`.

---

## Step 6 — OpenShell gateway

### 6a. Install

```bash
# Install CLI (for qnoe-ai user)
sudo -u qnoe-ai HOME=/home/qnoe-ai uv tool install -U openshell
sudo -u qnoe-ai bash -c 'echo "export PATH=\"/home/qnoe-ai/.local/bin:\$PATH\"" >> /home/qnoe-ai/.profile'

# Install gateway binary (separate deb package)
curl -LsSf -o /tmp/openshell.deb \
  "https://github.com/NVIDIA/OpenShell/releases/download/v0.0.59/openshell_0.0.59-1_arm64.deb"
sudo apt install -y /tmp/openshell.deb
# → installs /usr/bin/openshell-gateway

# Enable lingering so qnoe-ai services survive without login session
sudo loginctl enable-linger qnoe-ai
```

### 6b. Register inference provider

```bash
HOST_IP=$(hostname -I | awk '{print $1}')
sudo -u qnoe-ai HOME=/home/qnoe-ai bash -c "
  source /home/qnoe-ai/.profile
  openshell provider create \
    --name local-vllm --type openai \
    --credential OPENAI_API_KEY=none \
    --config OPENAI_BASE_URL=http://${HOST_IP}:8000/v1
  openshell inference set --provider local-vllm \
    --model /opt/qnoe-agent/models/hermes-3-70b-awq
"
```

### 6c. Install systemd service

```bash
sudo cp /opt/qnoe-agent/config/openshell-gateway.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now openshell-gateway
sudo systemctl status openshell-gateway   # → "Gateway Connected"
```

> **Key gotcha:** `generate-certs` must include `--server-san host.openshell.internal`. The supervisor inside the container connects to that hostname and performs strict TLS verification — missing this SAN causes `BadCertificate`. See `scripts/start_gateway.sh`.

---

## Step 7 — Build agent Docker image

```bash
sudo docker build -t qnoe-agent:latest /opt/qnoe-agent/
```

Test the sandbox:
```bash
sudo -u qnoe-ai HOME=/home/qnoe-ai bash -c '
  source /home/qnoe-ai/.profile
  openshell run qnoe-agent echo "sandbox ok"
'
# → "sandbox ok"
```

---

## Step 8 — Deploy agent code

```bash
# Clone or copy agent code to /opt/qnoe-agent/agent/
# (the full agent/ package from Z:\code\AI_Student\agent\)
sudo cp -r /path/to/agent/ /opt/qnoe-agent/agent/
sudo chown -R qnoe-ai:qnoe-ai /opt/qnoe-agent/agent/
```

The agent code structure:

| File | Role |
|---|---|
| `agent/main.py` | Entry point — Teams loop or dev REPL |
| `agent/graph.py` | LangGraph orchestrator — routes to sub-agents |
| `agent/llm.py` | AsyncOpenAI client → vLLM |
| `agent/prompts.py` | System prompts for all 6 sub-agents |
| `agent/retrieval.py` | Qdrant RAG — embed query → top-5 chunks |
| `agent/episodic.py` | SQLite L3 — log_event, get_episodic_context |
| `agent/state.py` | AgentState TypedDict |
| `agent/teams.py` | Teams connector (MSAL + Graph API polling) |
| `agent/ingest/` | Ingestion pipeline (see Step 9) |

---

## Step 9 — Ingest knowledge

### 9a. Clone all GitHub repos

```bash
cd /opt/qnoe-agent
export GITHUB_TOKEN=$(cat /opt/qnoe-agent/secrets/github_pat)
/opt/qnoe-agent/venv/bin/python agent/ingest/clone_org.py \
  --org QNOE-group \
  --dest /opt/qnoe-agent/repos \
  --token "$GITHUB_TOKEN"
```

### 9b. Ingest repos into Qdrant

```bash
export QDRANT_URL=http://localhost:6333
export EMBED_MODEL_PATH=/opt/qnoe-agent/models/nomic-embed
export AGENT_DATA_DIR=/opt/qnoe-agent/memory
export TRANSFORMERS_OFFLINE=1
export HF_HUB_OFFLINE=1

/opt/qnoe-agent/venv/bin/python -m agent.ingest.ingest_all \
  --repos-dir /opt/qnoe-agent/repos \
  --config /opt/qnoe-agent/config/repo_collections.yaml
```

This maps each repo to a Qdrant collection using `config/repo_collections.yaml` (substring pattern matching, first-match-wins).

### 9c. Ingest server documents

The lab network share `/ICFO/groups/NOE` must be mounted first (see **Appendix A**).

```bash
mkdir -p /home/yzamir/qnoe_server_data

setsid bash -c '
cd /opt/qnoe-agent
export QDRANT_URL=http://localhost:6333
export AGENT_DATA_DIR=/home/yzamir/qnoe_server_data
export EMBED_MODEL_PATH=/opt/qnoe-agent/models/nomic-embed
/opt/qnoe-agent/venv/bin/python -m agent.ingest.ingest_server \
  >> /tmp/server_ingest.log 2>&1
echo "Done: $?" >> /tmp/server_ingest.log
' &
```

- Runs as `yzamir` (not qnoe-ai) — only yzamir can read the network mount
- Separate manifest DB at `/home/yzamir/qnoe_server_data/episodic.db`
- All server content → `group-wide` collection
- Hash-based dedup — safe to restart, skips already-indexed files

Monitor progress:
```bash
tail -f /tmp/server_ingest.log
cat /tmp/empty_pdfs.log     # PDFs with no extractable text
cat /tmp/skipped_files.log  # Files skipped due to permission errors
```

**PDF handling strategy:**
- Non-papers (manuals, reports, books): fast extraction via `pypdf` (<1 second each)
- Papers (detected by folder "papers"/"manuscripts" in path, or Abstract + email/affiliation): `Docling` for layout-aware two-column extraction (~10–200 seconds each on CPU)

### 9d. Confirmed-paper Docling re-run

After the initial server ingestion, papers that were routed to `pypdf` (fast path) but should have been processed with Docling can be re-indexed. A review file (`runbook/SUSPECTED_PAPERS_REVIEW.md`) lists candidates; once a human marks them `Y`, run:

```bash
# Step 1 — build confirmed file lists (resolves truncated paths via find)
python runbook/generate_confirmed_lists.py
# Outputs:
#   /tmp/confirmed_papers_manuscripts.txt
#   /tmp/confirmed_papers_theses.txt
#   /tmp/confirmed_papers_books.txt

# Step 2 — re-ingest each list with Docling
for list_file in manuscripts theses books; do
  PYTHONPATH=/opt/qnoe-agent \
  QDRANT_URL=http://localhost:6333 \
  AGENT_DATA_DIR=/home/yzamir/qnoe_server_data \
  EMBED_MODEL_PATH=/opt/qnoe-agent/models/nomic-embed \
  /opt/qnoe-agent/venv/bin/python -m agent.ingest.run_ingest \
    --team group-wide \
    --repo-path / \
    --repo-name "server/papers" \
    --file-list /tmp/confirmed_papers_${list_file}.txt \
    >> /tmp/docling_confirmed_${list_file}.log 2>&1 &
done
```

Run as `yzamir` (network mount access). Each run is idempotent — already-indexed files are skipped via hash dedup. After completion, check `/tmp/one_chunk_files.log` for any files that yielded only 1 chunk (these may need OCR — see §9e).

### 9e. OCR backlog (scanned PDFs)

During ingestion, PDFs with < 200 characters of extractable text are logged to `/tmp/empty_pdfs.log` instead of being indexed. These are scanned PDFs with no text layer and require OCR.

**`ocr_queue.py`** handles this as an idempotent batch job:
- Reads `/tmp/empty_pdfs.log` (append-only, never modified by this script)
- Deduplicates entries
- Filters out files already in the manifest DB (the authoritative "done" set)
- Runs `ingest_directory` with `DOCLING_OCR=1` on pending files only

New entries are appended to the log by still-running workers; re-running the script picks them up automatically.

**GPU mode** (fastest — requires vLLM stopped):
```bash
# Stop vLLM first (it claims ~107 GB of 128 GB unified memory)
sudo systemctl stop vllm

# Dry run first to check counts
PYTHONPATH=/opt/qnoe-agent \
QDRANT_URL=http://localhost:6333 \
AGENT_DATA_DIR=/home/yzamir/qnoe_server_data \
EMBED_MODEL_PATH=/opt/qnoe-agent/models/nomic-embed \
DOCLING_DEVICE=cuda \
/opt/qnoe-agent/venv/bin/python -m agent.ingest.ocr_queue --dry-run

# Launch (background, append to log)
nohup bash -c '
  cd /opt/qnoe-agent
  PYTHONPATH=/opt/qnoe-agent \
  QDRANT_URL=http://localhost:6333 \
  AGENT_DATA_DIR=/home/yzamir/qnoe_server_data \
  EMBED_MODEL_PATH=/opt/qnoe-agent/models/nomic-embed \
  DOCLING_DEVICE=cuda \
  /opt/qnoe-agent/venv/bin/python -m agent.ingest.ocr_queue
' >> /tmp/ocr_queue.log 2>&1 &

# Restart vLLM when done
sudo systemctl start vllm
```

**CPU batch mode** (runs alongside vLLM — use `--batch` to limit per-run):
```bash
PYTHONPATH=/opt/qnoe-agent \
QDRANT_URL=http://localhost:6333 \
AGENT_DATA_DIR=/home/yzamir/qnoe_server_data \
EMBED_MODEL_PATH=/opt/qnoe-agent/models/nomic-embed \
DOCLING_DEVICE=cpu \
/opt/qnoe-agent/venv/bin/python -m agent.ingest.ocr_queue --batch 200
```

Add CPU batch mode to nightly cron for ongoing processing of newly discovered scanned PDFs (see Step 11).

---

## Step 9f — QCoDeS scanner (dedicated measurement index)

The QCoDeS scanner finds `.db` files containing QCoDeS measurement metadata, extracts run information, and indexes it into a dedicated `qcodes-runs` Qdrant collection. It also maintains a `qcodes_registry` SQLite table for structured queries.

**Dry run** (discover DBs without writing):
```bash
cd /opt/qnoe-agent && \
PYTHONPATH=/opt/qnoe-agent \
QDRANT_URL=http://localhost:6333 \
AGENT_DATA_DIR=/home/yzamir/qnoe_server_data \
EMBED_MODEL_PATH=/opt/qnoe-agent/models/nomic-embed \
/opt/qnoe-agent/venv/bin/python -m agent.ingest.qcodes_scanner --dry-run
```

**Full initial scan:**
```bash
cd /opt/qnoe-agent && \
PYTHONPATH=/opt/qnoe-agent \
QDRANT_URL=http://localhost:6333 \
AGENT_DATA_DIR=/home/yzamir/qnoe_server_data \
EMBED_MODEL_PATH=/opt/qnoe-agent/models/nomic-embed \
/opt/qnoe-agent/venv/bin/python -m agent.ingest.qcodes_scanner
```

**Verify:**
```bash
# Check qcodes-runs collection point count
curl -s http://localhost:6333/collections/qcodes-runs | python3 -c \
  "import sys,json; print(json.load(sys.stdin)['result']['points_count'])"

# Check registry row count
sqlite3 /home/yzamir/qnoe_server_data/episodic.db \
  "SELECT COUNT(*) FROM qcodes_registry; SELECT COUNT(DISTINCT db_path) FROM qcodes_registry;"

# Idempotency check — run again, expect 0 new runs
```

The scanner runs automatically as part of the nightly cron (Step 11) — no separate crontab entry needed.

---

## Step 10 — Wire Teams credentials

Set these environment variables in the agent service (see `config/qnoe-agent.service`):

```
TEAMS_TENANT_ID=<Azure tenant ID from IT>
TEAMS_CLIENT_ID=<Azure app client ID>
TEAMS_USERNAME=qnoe-ai@icfo.net
TEAMS_PASSWORD=<service account password>
```

Then enable the agent:
```bash
sudo systemctl enable --now qnoe-agent
sudo systemctl status qnoe-agent
```

---

## Step 11 — Nightly re-indexing (cron)

The nightly runner (`agent/indexing/nightly_run.py`) handles three tasks in sequence: Qdrant snapshot, GitHub repo re-indexing, and server document re-indexing.

Install in `yzamir`'s crontab (`crontab -e`):

```
0 2 * * * PYTHONPATH=/opt/qnoe-agent QDRANT_URL=http://localhost:6333 REPOS_DIR=/opt/qnoe-agent/repos AGENT_DATA_DIR=/home/yzamir/qnoe_server_data SERVER_ROOT=/ICFO/groups/NOE COLLECTIONS_CONFIG=/opt/qnoe-agent/config/repo_collections.yaml /opt/qnoe-agent/venv/bin/python -m agent.indexing.nightly_run >> /opt/qnoe-agent/logs/nightly_reindex.log 2>&1
```

> **Note:** `PYTHONPATH=/opt/qnoe-agent` is required — the venv Python will not find the `agent` package without it.

Optionally, add OCR batch processing of newly discovered scanned PDFs (runs alongside vLLM using CPU):

```
30 3 * * * PYTHONPATH=/opt/qnoe-agent QDRANT_URL=http://localhost:6333 AGENT_DATA_DIR=/home/yzamir/qnoe_server_data EMBED_MODEL_PATH=/opt/qnoe-agent/models/nomic-embed DOCLING_DEVICE=cpu /opt/qnoe-agent/venv/bin/python -m agent.ingest.ocr_queue --batch 200 >> /opt/qnoe-agent/logs/ocr_queue_nightly.log 2>&1
```

---

## Health checks

```bash
# Is vLLM up?
curl http://localhost:8000/health

# Is Qdrant up?
curl http://localhost:6333/healthz

# How many vectors in each collection?
for col in qtm photocurrent qed superconductivity qsim xchiral group-wide; do
  count=$(curl -s http://localhost:6333/collections/$col | python3 -c \
    "import sys,json; print(json.load(sys.stdin)['result']['points_count'])")
  echo "$col: $count"
done

# Is the gateway running?
sudo systemctl status openshell-gateway

# Is the agent running?
sudo systemctl status qnoe-agent

# Check agent logs
sudo journalctl -u qnoe-agent -f
```

---

## After a reboot

Services that auto-start (systemd enabled):
- `vllm.service` ✓
- `openshell-gateway.service` ✓
- `qdrant` Docker container (`--restart unless-stopped`) ✓

Services that need manual restart:
- `qnoe-agent.service` — enable once Teams credentials are wired: `sudo systemctl enable --now qnoe-agent`

Network mount that drops on reboot:
- `/ICFO/groups/NOE` — normally remounts via `pam_mount` on interactive login. If it doesn't, see **Appendix A**.

---

## Appendix A — Mounting the ICFO network share

The NOE group share is a CIFS mount from `files.icfo.es`. It normally mounts automatically via `pam_mount` when yzamir logs in interactively.

If it's missing after a reboot:
```bash
sudo mount -t cifs "//files/groups/NOE" /ICFO/groups/NOE -o username=yzamir,domain=ICFONET
# Enter yzamir's ICFO domain password when prompted
```

Verify:
```bash
ls /ICFO/groups/NOE/
```

**To make it permanent** (survives reboots without interactive login), ask IT to add credentials to `/etc/samba/credentials.icfo` and add an fstab entry:
```
//files.icfo.es/groups/NOE  /ICFO/groups/NOE  cifs  credentials=/etc/samba/credentials.icfo,uid=13180,gid=35001,domain=icfonet,vers=2.1,_netdev,noauto,x-systemd.automount  0 0
```

### CIFS performance tuning

With 3+ concurrent processes reading from the CIFS mount, default mount options cause significant contention (slow directory scans, read throughput drops). Apply these options to improve throughput without remounting:

```bash
sudo mount -o remount,cache=loose,rsize=131072,wsize=131072,actimeo=60 /ICFO/groups/NOE
```

- `cache=loose` — enables aggressive client-side page cache (safe for read-mostly workloads; other writers won't see updates for up to `actimeo` seconds)
- `rsize=131072` / `wsize=131072` — 128 KB read/write buffer (vs default 65 KB)
- `actimeo=60` — caches directory metadata for 60 seconds, reducing round-trips to the CIFS server

The remount takes effect immediately for all running processes — no restart needed. To make these options permanent, add them to the fstab entry above.

---

## Monthly maintenance — oversized PDF processing

**Schedule:** First Sunday of each month (or when convenient). Duration: 2-4 hours.

**Why:** Nightly ingestion skips PDFs/PPTX/DOCX files larger than 50 MB to avoid OOM while vLLM occupies ~70 GB of memory. These files are logged to `/opt/qnoe-agent/logs/oversized_files.log`. Once a month, stop vLLM, process the backlog with full memory available, then restart.

**Procedure:**

```bash
# 1. Check if there's anything to process
wc -l /opt/qnoe-agent/logs/oversized_files.log
# If 0 lines → nothing to do, skip this month

# 2. Stop agent + vLLM (frees ~70 GB)
sudo systemctl stop qnoe-agent
sudo systemctl stop vllm

# 3. Extract file paths from the log (format: timestamp\tpath\tsize)
cut -f2 /opt/qnoe-agent/logs/oversized_files.log > /tmp/oversized_paths.txt

# 4. Process oversized files with Docling (full memory available)
PYTHONPATH=/opt/qnoe-agent QDRANT_URL=http://localhost:6333 \
  AGENT_DATA_DIR=/home/yzamir/qnoe_server_data \
  DOCLING_MAX_FILE_BYTES=0 \
  FILE_TIMEOUT=3600 \
  FAILED_FILES_LOG=/tmp/failed_oversized.log \
  /opt/qnoe-agent/venv/bin/python -m agent.ingest.run_ingest \
  --team group-wide \
  --file-list /tmp/oversized_paths.txt \
  --repo-name server/oversized \
  >> /tmp/monthly_oversized.log 2>&1

# 5. Check results
tail -5 /tmp/monthly_oversized.log
cat /tmp/failed_oversized.log  # any that still failed

# 6. Clear the processed log
> /opt/qnoe-agent/logs/oversized_files.log

# 7. Restart vLLM + agent
sudo systemctl start vllm
# Wait ~5 min for vLLM cold start (FlashInfer JIT)
curl http://localhost:8000/health  # confirm it's up
sudo systemctl start qnoe-agent
```

**Files that still fail** (in `/tmp/failed_oversized.log`) are likely corrupted or truly enormous (>500 MB). Review manually and decide whether to skip permanently.

---

## Appendix B — Key gotchas (learned the hard way)

| Issue | Fix |
|---|---|
| vLLM fails with `FileNotFoundError: ninja` | Add `/opt/qnoe-agent/venv/bin` to `PATH` in `vllm.service` |
| vLLM first startup takes ~5–7 minutes | FlashInfer JIT-compiles kernels on cold start; cached after first run |
| `nomic-embed` must use CPU | GPU is fully occupied by vLLM 70B. Set `device="cpu"` in `embed.py` |
| Qdrant v1.18 API change | Use `query_points()` not `search()` |
| vLLM model ID must be full path | `/opt/qnoe-agent/models/hermes-3-70b-awq` — short name doesn't work |
| PDF temp files on network mount fail | Write temp files to `/tmp` via `tempfile.NamedTemporaryFile`, not alongside the source PDF |
| Server ingestion must run as `yzamir` | Only yzamir can read `/ICFO/groups/NOE`; use `AGENT_DATA_DIR=/home/yzamir/qnoe_server_data` |
| Manifest DB read-only for yzamir | `episodic.db` created by qnoe-ai has group `root`. Fix: `sudo chgrp qnoe-ai /opt/qnoe-agent/memory/episodic.db /opt/qnoe-agent/memory/` |
| Docling uses GPU by default | Set `AcceleratorDevice.CPU` and `do_ocr=False` in pipeline options |
| Docling v2 API changed | Use `DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(...)})` — NOT `pipeline_options=` kwarg |
| OpenShell TLS `BadCertificate` | Add `--server-san host.openshell.internal` to `generate-certs` |
| `uv` reads wrong config when run as qnoe-ai | `cd /tmp` first — it picks up `uv.toml` from the current directory |
| Docling reloads models for each PDF | Cache `DocumentConverter` as a global variable in `splitter.py` |
| ICFO mount drops on reboot | Mount via `pam_mount` only works on interactive login; needs fstab for unattended use |
| Stale chunks accumulate in Qdrant | On file change, old Qdrant point IDs must be deleted before re-indexing — `run_ingest.py` does this via `_delete_old_chunks()` |
| PDFs return only 1 chunk | pypdf extracts metadata-only from scanned PDFs (<2000 chars). Threshold routes these to Docling. Truly scanned PDFs need OCR (see B5 in `PHASE2_BACKLOG.md`) |
| SIM-Meep excluded from ingestion | Large FDTD repo not useful for RAG. Listed in `exclude:` in `repo_collections.yaml` |
| PAT exposed in `git pull` error logs | `git pull` with embedded PAT in remote URL logs the full URL on failure. Sanitize: `stderr.replace(pat, "***")` before logging |
| SQLite connection leak on schema error | In `_chunk_qcodes_db`, a failed `CREATE TABLE` in the `except` block returns without closing `conn`. Split the outer `try` so `conn.close()` runs in `finally` |
| CIFS contention with 3+ concurrent readers | 3+ processes on the network mount causes read slowdowns. Remount with `cache=loose,rsize=131072,wsize=131072,actimeo=60` — takes effect immediately (see Appendix A) |
| GPU OCR conflicts with vLLM | `DOCLING_DEVICE=cuda` + `DOCLING_OCR=1` requires vLLM stopped first — vLLM claims ~107 GB of 128 GB unified memory. Use `DOCLING_DEVICE=cpu` when vLLM is running |
| `manifest_db` parameter missing from old `run_ingest.py` | DGX may have an older version without the `manifest_db` parameter. Deploy updated `run_ingest.py` via `scp` + `sudo cp` |
| `generate_confirmed_lists.py` hardcodes review file path | Script defaults to `/opt/qnoe-agent/runbook/SUSPECTED_PAPERS_REVIEW.md`. If directory doesn't exist, patch with `sed` before running or pass an env var |

---

## Appendix D — PDF extraction strategy

PDFs are handled with a hybrid approach to balance speed vs. quality:

```
PDF file
  │
  ├─ pypdf extracts text
  │
  ├─ < 2000 chars (< 1 page)?  ──→  Docling (scanned / image-heavy)
  │
  ├─ Paper detected?            ──→  Docling (layout-aware, two-column)
  │   • path contains "papers" or "manuscripts"
  │   • has "abstract" + email address
  │   • has "abstract" + affiliation word
  │   • has "abstract" + avg line length < 65 chars
  │
  └─ Otherwise                  ──→  pypdf result used directly (fast)
```

**Packages required:** `pypdf`, `docling` (both in venv)

**Known limitation:** Truly scanned PDFs (no text layer) produce 0–1 chunks even with Docling when `do_ocr=False`. These are logged to `/tmp/empty_pdfs.log`. Process them with `ocr_queue.py` (see §9e).

**Enabling OCR at runtime** (no code change needed): set `DOCLING_OCR=1` in the environment before launching any ingestion command. `splitter.py` reads this flag in `_get_pdf_converter()` and passes `do_ocr=True` to the Docling pipeline. Combine with `DOCLING_DEVICE=cuda` for GPU acceleration (requires vLLM stopped).

**Re-indexing PDFs only** (after threshold change or strategy update):
```bash
python -m agent.ingest.ingest_all --force-ext .pdf
```

---

## Appendix C — Directory reference

```
/opt/qnoe-agent/
├── agent/              # Python agent package (the AI code)
│   └── ingest/         # Ingestion pipeline
├── config/             # Service files, policy, triggers
│   ├── vllm.service
│   ├── openshell-gateway.service
│   ├── qnoe-agent.service
│   ├── sandbox-policy.yaml
│   └── repo_collections.yaml
├── scripts/            # Startup scripts called by systemd
│   ├── start_vllm.sh
│   ├── start_gateway.sh
│   └── start_agent.sh
├── models/
│   ├── hermes-3-70b-awq/   # 40 GB — main LLM
│   └── nomic-embed/        # 2 GB — embedding model
├── qdrant_data/        # Qdrant vector DB storage
├── memory/
│   ├── episodic.db     # SQLite — event log + audit
│   └── checkpoints.db  # LangGraph session state
├── secrets/
│   └── github_pat      # chmod 600
├── logs/               # Gateway and agent logs
├── repos/              # Cloned GitHub repos
├── Dockerfile          # Agent sandbox image
└── launch_sandbox.sh   # Manual sandbox test script
```

## B7-OS: Hermes gateway in the OpenShell sandbox (since 2026-07-14)

- **Production unit:** `qnoe-hermes-sandbox.service` (enabled). Start/stop via systemctl only;
  it wraps `openshell sandbox create` and ExecStopPost deletes the container.
- **Rollback to systemd mechanism:** `sudo systemctl start qnoe-hermes` (Conflicts= stops the
  sandbox unit). Flip back: `sudo systemctl start qnoe-hermes-sandbox`.
- **Verify enforcement:** `sudo systemctl start qnoe-b7-sandbox-test` → `logs/b7_probe.log`
  all-PASS (24 checks). Systemd-mechanism probe: `qnoe-b7-test`.
- **Rebuild image** (only when OS libs change): `mkdir -p /tmp/qnoe-hermes-build && cp
  Dockerfile.hermes /tmp/qnoe-hermes-build/Dockerfile && docker build -t qnoe-hermes:0.1
  /tmp/qnoe-hermes-build` — NEVER build with /opt/qnoe-agent as context (streams models/venvs).
- **teams.env rotation:** file is bind-mounted by inode → after rotating secrets/teams.env run
  `sudo systemctl restart qnoe-hermes-sandbox`.
- **Audit/L7 denials:** `sudo systemctl status openshell-gateway --no-pager -n 200 | grep -iE
  '403|denied'`; gateway state db at `~qnoe-ai/.local/state/openshell/gateway/openshell.db`.
- **Confinement contract:** `config/sandbox-policy.yaml` (single source of truth) +
  `config/hermes-sandbox-mounts.json`. New writable/readable path ⇒ update policy + mounts +
  `scripts/b7_probe.sh` together, redeploy, re-run both probes.
- **Gateway restart choreography:** restarting `openshell-gateway` kills the sandbox relay; the
  unit self-heals in ~60s (expected). Expect one auto-resume echo reply after any restart.
