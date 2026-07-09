# DGX Setup Log
*Tracking actual setup progress on the DGX Spark (pcl2013, 10.3.8.21)*

> Claude Code memory: [[memory/infrastructure]] · Deploy patterns: [[memory/deploy-patterns]] · Mistakes: [[memory/mistakes]]

---

## Phase 0 — Hardware & OS Readiness

**Date:** 2026-06-05

| Check | Result | Status |
|---|---|---|
| OS | Ubuntu 24.04.4 LTS (Noble Numbat) | PASS |
| GPU | NVIDIA GB10 Grace Blackwell | PASS |
| Driver | 580.159.03 (target: ≥ 570) | PASS |
| CUDA | 13.0 (target: ≥ 12.4) | PASS |
| Python | 3.12.3 (target: 3.11+) | PASS |
| NVMe free | 3.5 TB free of 3.7 TB (target: ≥ 2 TB) | PASS |
| HuggingFace reachable | HTTP 200 | PASS |

**Network mounts (already configured on OS):**
| Mount | Path | Notes |
|---|---|---|
| Personal home | `/ICFO/smbhome/yzamir` | 30 GB, 33% used |
| ICFO general | `/ICFO/general` | 16 TB, 79% used |
| NOE group share | `/ICFO/groups/NOE` | 3.2 TB, 92% used — nearly full, flag to Frank |

**Notes:**
- DGX_SETUP.md Step 9 (network mounts) largely complete — mounts pre-configured.
- Agent data, Qdrant storage, and `.agent_trash/` must live on local NVMe (`~/qnoe-agent/` for now, `/opt/qnoe-agent/` after migration).
- Ubuntu 24.04 (not 22.04 as assumed in docs) — no impact, all steps remain valid.

**Phase 0 hardware check: COMPLETE**

---

## Phase 0 — Python Environment (Step 2)

**Date:** 2026-06-05

| Check | Result | Status |
|---|---|---|
| `uv` installed | 0.11.19, aarch64 | PASS |
| venv created | `~/qnoe-agent/venv` (Python 3.12.3) | PASS |
| venv on PATH | `/home/yzamir/qnoe-agent/venv/bin/python` | PASS |

**Notes:**
- No sudo access — using `~/qnoe-agent/` instead of `/opt/qnoe-agent/`. IT request pending.
- Single-user setup for now — agent runs as `yzamir`. `qnoe-ai` service account created by IT on 2026-06-09; shell hardening (Step D) pending.
- Migration to `/opt/qnoe-agent/` required before go-live (see TODO.md).

**Step 2: COMPLETE**

---

## Phase 0 — vLLM Installation (Step 3)

**Date:** 2026-06-05

| Check | Result | Status |
|---|---|---|
| vLLM installed | 0.22.1 (189 packages) | PASS |
| PyTorch | 2.11.0 | PASS |
| GPU visible to vLLM | `from vllm import LLM` → ok | PASS |

**Step 3: COMPLETE**

---

## Phase 0 — Model Download + vLLM Serve (Step 4)

**Date:** 2026-06-05

| Check | Result | Status |
|---|---|---|
| Model downloaded | `mbley/NousResearch-Hermes-3-Llama-3.1-70B-AWQ` → `~/qnoe-agent/models/hermes-3-70b-awq` (39.8 GB) | PASS |
| vLLM serve (awq) | Failed — `torch.bfloat16` not supported for `awq` | FAIL |
| vLLM serve (awq_marlin) | Failed — `Python.h` missing (Triton cannot compile CUDA utils) | FAIL |
| vLLM serve (awq_marlin --enforce-eager) | Failed — same `Python.h` error in sampler Triton kernel | FAIL |

**Root cause:** `python3.12-dev` not installed — Triton needs system Python headers to JIT-compile its CUDA utilities. This affects all vLLM invocations regardless of compilation flags.

**Workarounds attempted:**
- `--quantization awq_marlin` — correct quantization, but blocked on same Triton issue
- `--enforce-eager` — does not bypass Triton in the sampler path

**Unblocking options (all require IT):**
- `sudo apt install python3.12-dev` → native vLLM works
- Add `yzamir` to `docker` group → can run vLLM Docker container instead
- `sudo` access → can do both of the above

**Resolution (2026-06-08):** IT granted sudo to `yzamir`. Ran `sudo apt install python3.12-dev`. vLLM now serves correctly.

**Working launch command:**
```bash
vllm serve ~/qnoe-agent/models/hermes-3-70b-awq --host 127.0.0.1 --port 8000 --quantization awq_marlin --max-model-len 32768
```

| Check | Result | Status |
|---|---|---|
| vLLM serving | `http://127.0.0.1:8000` responding | PASS |
| Test completion | "What is graphene?" → correct 113-token response | PASS |
| KV cache | 70.13 GiB available, 229,792 token capacity | PASS |
| Memory used | 37.09 GiB for weights + 0.54 GiB CUDA graphs | PASS |

**Notes:**
- First inference has a Triton JIT latency spike (`_compute_slot_mapping_kernel`) — expected on cold start, cached after first use.
- Throughput on first request: 2.4 tokens/s prompt, 0.3 tokens/s generation (cold). Will improve after warmup.

**Step 4: COMPLETE**

---

## Phase 0 — Embedding Model (Step 6)

**Date:** 2026-06-05

| Check | Result | Status |
|---|---|---|
| Model downloaded | `nomic-ai/nomic-embed-text-v1.5` → `~/qnoe-agent/models/nomic-embed` (2.22 GB) | PASS |
| `sentence-transformers` installed | via `uv pip install` | PASS |
| Embedding test | `encode('test sentence')` → vector dim 768 | PASS |

**Notes:**
- Vector dim 768 matches all Qdrant collection configs exactly.
- Model downloads extra files from HuggingFace at runtime (`nomic-bert-2048` custom code). For air-gapped production, pin a revision and pre-cache these files.

**Step 6: COMPLETE**

---

## Phase 0 — Qdrant Deployment (Step 7)

**Date:** 2026-06-05

| Check | Result | Status |
|---|---|---|
| Qdrant binary | v1.18.2, aarch64, `~/qnoe-agent/bin/qdrant` | PASS |
| Qdrant running | Port 6333, data at `~/qnoe-agent/qdrant_data/` | PASS |
| Collections created | group-wide, qed, superconductivity, photocurrent, qtm, qsim, xchiral (7 of 15) | PASS |

**Notes:**
- Docker not available (user not in docker group — IT request pending), used standalone binary instead.
- `--storage-path` flag removed in v1.18 — storage path set via `QDRANT__STORAGE__STORAGE_PATH` env var.
- Qdrant started in background; not yet a systemd service (blocked on sudo).
- Start command: `QDRANT__STORAGE__STORAGE_PATH=$HOME/qnoe-agent/qdrant_data ~/qnoe-agent/bin/qdrant &`
- 7 of 7 RAG collections created (prose/code split dropped — nomic-embed used for all content). `episodic_memory` (Mem0) and `qcodes-runs` collections to be added later.

**Step 7: COMPLETE (partial — 7/15 collections)**

---

## Phase 0 — SQLite Episodic Store (Step 8)

**Date:** 2026-06-05

| Check | Result | Status |
|---|---|---|
| DB created | `~/qnoe-agent/memory/episodic.db` | PASS |
| `events` table | Created | PASS |
| `audit_log` table | Created | PASS |

**Step 8: COMPLETE**

---

## Summary — Current State

| Step | Description | Status |
|---|---|---|
| 1 | Hardware + OS readiness | COMPLETE |
| 2 | Python environment (`uv` + venv) | COMPLETE |
| 3 | vLLM installation | COMPLETE |
| 4 | Hermes 3 70B download + serve | COMPLETE |
| 5 | Inference benchmark | COMPLETE (baseline 3.53/5 — see benchmark/benchmark_scores.md) |
| 6 | Embedding model | COMPLETE |
| 7 | Qdrant deployment | COMPLETE |
| 8 | SQLite episodic store | COMPLETE |
| 9 | Network mounts | COMPLETE (pre-configured) |
| 10 | GitHub agent account + PAT | COMPLETE (dev PAT, personal account) |
| B | Migration to `/opt/qnoe-agent/` + `qnoe-ai` ownership | COMPLETE (2026-06-09) |
| D-prep | Docker group + NVIDIA container runtime | COMPLETE (2026-06-09) |
| D-openshell | OpenShell CLI + gateway installed | COMPLETE (2026-06-11) |
| 11 | OpenShell sandbox (provider, Dockerfile, policy, test) | COMPLETE (2026-06-11) |
| 12 | Systemd services | PARTIAL (2026-06-12) — vllm + gateway running; agent disabled pending Phase 1 |
| **Phase 1** | **Agent code written and installed** | **PARTIAL (2026-06-12) — core working; ingestion + Teams pending** |

**Docker bridge IP confirmed: `172.18.0.1`** (from gateway log — used in Qdrant network policy)

**Current running services on DGX:**
| Service | State | Notes |
|---|---|---|
| `vllm.service` | enabled, running | Hermes 3 70B AWQ at port 8000; auto-starts on reboot |
| `openshell-gateway.service` | enabled, running | OpenShell gateway at port 17670; auto-starts on reboot |
| `qnoe-agent.service` | **disabled** | Service file installed; enable after ingestion + Teams credentials wired in |
| `qdrant` (Docker) | running | Port 6333; started manually — add to systemd (see below) |

**Next unblocked actions:**
1. ~~Clone QTM + Photocurrent repos → run ingestion pipeline → Qdrant populated~~ **DONE (2026-06-16)**
2. ~~Wire in Teams credentials~~ **DONE (2026-06-19)** — all 4 env vars in `teams.env`. No MFA (confirmed by IT).
3. Restart vLLM (waiting for background process to finish) → `sudo systemctl enable --now qnoe-agent`
4. Step C (GitHub PAT) once `qnoe-ai` GitHub account is opened
5. ~~Cron job for nightly re-indexing~~ **DONE (2026-06-16)**

---

## Phase 0 — OpenShell Sandbox (Step 11)

**Date:** 2026-06-11

| Check | Result | Status |
|---|---|---|
| Inference provider registered | `local-vllm` → vLLM at host LAN IP:8000 | PASS |
| Model ID | `/opt/qnoe-agent/models/hermes-3-70b-awq` (full path required) | PASS |
| Dockerfile written | `/opt/qnoe-agent/Dockerfile` (python:3.12-slim, sandbox uid 1000660000) | PASS |
| sandbox-policy.yaml written | `/opt/qnoe-agent/config/sandbox-policy.yaml` | PASS |
| Docker image built | `qnoe-agent:latest` | PASS |
| Gateway TLS setup | `generate-certs` with mTLS, `OPENSHELL_LOCAL_TLS_DIR` mode | PASS |
| Sandbox launch | `Created sandbox: qnoe-agent` → `sandbox ok`, `Python 3.12.13` | PASS |

**Root cause of BadCertificate TLS error (resolved 2026-06-11):**
The supervisor inside the container connects to `https://host.openshell.internal:17670/` and performs strict hostname verification. The server cert generated with only `--server-san 127.0.0.1 --server-san 172.18.0.1` was missing `host.openshell.internal` as a DNS SAN. Fix: add `--server-san host.openshell.internal` to `generate-certs`. Note: `openssl s_client` does NOT verify hostnames by default and returns OK even without this SAN — it is not a reliable proxy for supervisor compatibility.

**Step 11: COMPLETE**

---

## Phase 0 — Systemd Services (Step 12)

**Date:** 2026-06-12

| Check | Result | Status |
|---|---|---|
| `vllm.service` written | `/etc/systemd/system/vllm.service` | PASS |
| `openshell-gateway.service` written | `/etc/systemd/system/openshell-gateway.service` | PASS |
| `qnoe-agent.service` written | `/etc/systemd/system/qnoe-agent.service` | PASS |
| Scripts installed | `/opt/qnoe-agent/scripts/start_vllm.sh`, `start_gateway.sh`, `start_agent.sh` | PASS |
| vllm.service running | Port 8000 responding, model loaded | PASS |
| openshell-gateway.service running | Gateway Connected, TLS healthy | PASS |
| qnoe-agent.service | Disabled — `python -m agent.main` fails (no agent code yet) | DEFERRED |

**Issues encountered and resolved:**

1. **`status=203/EXEC` on start_vllm.sh**: Heredoc written with leading spaces before `#!/bin/bash`. The shebang must be the first two characters of the file. Fix: use `printf` instead of heredoc with indentation.

2. **`FileNotFoundError: 'ninja'`**: FlashInfer JIT-compiles a CUDA sampling kernel at first startup using `ninja`. The `Environment=PATH=` line was accidentally dropped when rewriting the service file via `sudo tee`. Without `/opt/qnoe-agent/venv/bin` in PATH, `ninja` (installed in the venv) is not found. Fix: always include `Environment=PATH=/opt/qnoe-agent/venv/bin:...` in `vllm.service`. After first successful start, the kernel is cached and subsequent starts are faster.

3. **First startup takes ~7 minutes**: Model load (268s) + torch.compile (21s) + CUDA graph capture + FlashInfer JIT compile on first run. Subsequent starts use cached compile artifacts and take ~5 minutes.

**Step 12: PARTIAL — vllm + gateway complete; agent deferred to Phase 1**

---

## Phase 1 — Repo Ingestion (2026-06-15/16)

### GitHub repos cloned and ingested

All 41 repos from QNOE-group org cloned to `/opt/qnoe-agent/repos/` using `clone_org.py`.
Mapping of repos → Qdrant collections confirmed in `REPO_MAPPING.md` and encoded in `config/repo_collections.yaml`.

**Qdrant collection state after ingestion:**
| Collection | Points | Notes |
|---|---|---|
| qed | 9,676 | Largest — many QED repos |
| group-wide | 3,152+ | Includes partial server ingestion |
| qsim | 2,749 | |
| photocurrent | 1,028 | |
| superconductivity | 227 | Re-run required (crashed mid-way, fixed with --force) |
| qtm | 251 | |
| xchiral | 0 | No XCHIRAL repos exist yet |

**Key ingestion decisions:**
- `repo_collections.yaml`: substring pattern matching, first-match-wins, default→group-wide
- Removed `PhQH` pattern from photocurrent (matched `QED-phqh` incorrectly)
- `MIT_BLG-*` repos → photocurrent (added before generic `BLG` → qed)
- GRASP, SIM-Meep, SIM-kwant, L208_Opticool, MoO3-hBN-MoO3, gvAI → group-wide
- FTIR-L205-RapidScan, Elisa-codes → qed
- Notebooks (`SIM-Nbandstructure`, `SIM-Meep`) → group-wide, not qsim

### Key fixes during ingestion
- nomic-embed must run on CPU (`device="cpu"`) — GPU fully occupied by vLLM 70B
- Qdrant v1.18 uses `query_points()` not `search()`
- vLLM model ID is full path `/opt/qnoe-agent/models/hermes-3-70b-awq`
- `TRANSFORMERS_OFFLINE=1 HF_HUB_OFFLINE=1` needed to prevent internet calls during inference
- `ensure_schema()` must run before LangGraph graph

---

## Phase 1 — Server Document Ingestion (2026-06-16)

### New ingestion pipeline for `/ICFO/groups/NOE`

Built `agent/ingest/ingest_server.py` to ingest documents from the lab network share.

**Supported file types:**
| Type | Extension | Parser |
|---|---|---|
| PDF | `.pdf` | Docling (CPU-only, no OCR) |
| PowerPoint | `.pptx` | python-pptx (slide-by-slide) |
| Word | `.docx` | python-docx (paragraph chunks) |
| Markdown / text | `.md`, `.txt` | paragraph splitting |
| QCoDeS databases | `.db` | SQLite metadata extraction (1 chunk/run) |

**Target folders (all → group-wide collection):**
Lab_Instruments, Manuscripts, Meetings, Notebook, Notebooks, Papers & Books, Posters, Presentation, Presentations, Projects, Spectromag, Theses & reports

**Notebook folder contents** (reviewed before including):
- 16k `.py`, 8k `.txt`, 3.5k `.pptx`, 2.6k `.db` (QCoDeS), 1.7k `.pdf`
- Binary data files (`.dump`, `.dat`, `.mat`, images) automatically skipped by extension filter
- Included: 2,636 QCoDeS databases contain experimental metadata (sample name, parameters, timestamps) — high value for agent

**Key decisions:**
- Docling runs CPU-only (`do_ocr=False`, `AcceleratorDevice.CPU`) — GPU occupied by vLLM
- `DocumentConverter` cached as global (`_pdf_converter`) — avoids reloading models per file
- Empty PDFs logged to `/tmp/empty_pdfs.log` for post-run review
- Server ingestion manifest stored at `/home/yzamir/qnoe_server_data/episodic.db` (yzamir-owned, separate from agent's episodic.db)
- Runs as `yzamir` (not qnoe-ai) — only yzamir has access to the network mount
- yzamir added to `qnoe-ai` group + `chmod g+w /opt/qnoe-agent/memory/` for shared DB access

**Packages installed:**
```bash
uv pip install python-pptx python-docx docling
```

**Status:** Running as of 2026-06-16, PID 2727203. Expected to complete overnight (~20k files).

**To check progress:**
```bash
tail -f /tmp/server_ingest.log
cat /tmp/empty_pdfs.log   # PDFs that produced no text
```

**To restart after interruption:**
```bash
setsid bash -c '
cd /opt/qnoe-agent
export QDRANT_URL=http://localhost:6333
export AGENT_DATA_DIR=/home/yzamir/qnoe_server_data
export EMBED_MODEL_PATH=/opt/qnoe-agent/models/nomic-embed
/opt/qnoe-agent/venv/bin/python -m agent.ingest.ingest_server >> /tmp/server_ingest.log 2>&1
echo "Done: $?" >> /tmp/server_ingest.log
' &
```
Hash-based dedup means re-running is safe — already-indexed files are skipped.

---

## Phase 1 — Nightly Cron (2026-06-16)

### What was done

Created `agent/indexing/nightly_run.py` — an extensible maintenance runner with a simple task registry.

**Design:** each task is a plain `def task_<name>() -> None` appended to `TASKS`. The runner executes them in order, logs per-task timing and result, and continues on failure. To add a new task: write the function, append to `TASKS`. No other changes needed.

**Tasks registered:**
| Task | What it does |
|---|---|
| `task_qdrant_snapshot` | Queries Qdrant for current collection list (dynamic — no hardcoded names), snapshots all, prunes snapshots older than 7 days |
| `task_index_repos` | Incremental re-index of all GitHub repos in `/opt/qnoe-agent/repos/` |
| `task_index_server` | Incremental re-index of NOE server folders in `/ICFO/groups/NOE/` |
| `task_scan_qcodes` | Scans for QCoDeS databases, updates registry + `qcodes-runs` collection |
| `task_orphan_cleanup` | Removes Qdrant chunks for files missing from disk for 7+ days (7-day grace period, mount guard) |

**Cron entry installed** (in yzamir's crontab — runs as yzamir for NOE mount access):
```
0 2 * * * PYTHONPATH=/opt/qnoe-agent QDRANT_URL=http://localhost:6333 REPOS_DIR=/opt/qnoe-agent/repos AGENT_DATA_DIR=/opt/qnoe-agent/memory SERVER_DATA_DIR=/home/yzamir/qnoe_server_data SERVER_ROOT=/ICFO/groups/NOE COLLECTIONS_CONFIG=/opt/qnoe-agent/config/repo_collections.yaml /opt/qnoe-agent/venv/bin/python -m agent.indexing.nightly_run >> /opt/qnoe-agent/logs/nightly_reindex.log 2>&1
```

**Key fix:** `PYTHONPATH=/opt/qnoe-agent` is required — the venv Python doesn't have `/opt/qnoe-agent` on its path by default.

**Dry-run verified:** `--dry-run` confirmed all 3 tasks load and are registered correctly.

**Step 12 (nightly cron): COMPLETE**

---

## Nightly Cron — Permission Fix (2026-06-23)

The nightly cron job never produced output — `/opt/qnoe-agent/logs/nightly_reindex.log` did not exist. Root cause: the cron runs as `yzamir`, but `/opt/qnoe-agent/logs/` was owned by `qnoe-ai:root` with mode `755`, so `yzamir` could not write to it.

**Fix applied:**
```bash
sudo usermod -aG qnoe-ai yzamir
sudo chmod -R g+w /opt/qnoe-agent/logs/
sudo chmod g+w /opt/qnoe-agent/repos/
sudo chmod g+w /opt/qnoe-agent/memory/
```

The group change takes effect on next login; the cron will pick it up at the next 02:00 run (cron starts a fresh session). The nightly run should produce its first log file tonight.

---

## Phase 1 — Ingestion Pipeline Improvements (2026-06-17)

### Issues found and fixed

**1. PDF temp file permission error**
Writing `.pdf.md.tmp` files alongside source PDFs on the read-only ICFO network mount caused `Permission denied`. Fixed in `splitter.py` by writing temp files to `/tmp` via `tempfile.NamedTemporaryFile`.

**2. Read-only manifest DB**
`/opt/qnoe-agent/memory/episodic.db` was owned `qnoe-ai:root`. yzamir is in the `qnoe-ai` group but group was `root`, so yzamir only had read permission. Fixed with:
```bash
sudo chgrp qnoe-ai /opt/qnoe-agent/memory/episodic.db /opt/qnoe-agent/memory/
```

**3. Skipped files not logged**
Files that failed hashing (permission denied on network mount) were only warned in the log. Added `/tmp/skipped_files.log` — one entry per file with the error reason. Implemented in `run_ingest.py`.

**4. Stale chunks on file update**
When a file changes, re-indexing produced new chunks with new UUIDs but never deleted the old chunks from Qdrant. Fixed: `run_ingest.py` now calls `_delete_old_chunks()` before re-indexing, using the old point IDs stored in the manifest.

**5. SIM-Meep excluded**
SIM-Meep is a large Meep FDTD simulation repo not useful for RAG. Added to `exclude:` list in `repo_collections.yaml`. The `ingest_all.py` now reads this list and skips those repos.

**6. `--force-ext` flag added**
New flag for `ingest_all.py` and `run_ingest.py`: re-indexes only files with specified extensions. Example:
```bash
python -m agent.ingest.ingest_all --force-ext .pdf
```
Useful for re-running just PDFs after changing the PDF extraction strategy.

### Hybrid PDF extraction strategy

Replaced Docling-only PDF handling with a hybrid approach:

| Condition | Extractor | Why |
|---|---|---|
| pypdf gets ≥2000 chars AND not a paper | pypdf | Fast (<1s), good for manuals/books/reports |
| pypdf gets <2000 chars | Docling | Likely scanned — pypdf only got metadata |
| Paper detected | Docling | Layout-aware, handles two-column correctly |
| pypdf gets nothing | Docling | Fallback for image-only PDFs |

**Paper detection heuristic** (any of these → Docling):
1. Path contains "papers" or "manuscripts"
2. Text has "abstract" + email address (`@`)
3. Text has "abstract" + affiliation keyword (university, institute, department...)
4. Text has "abstract" + average line length < 65 chars (two-column layout)

**Threshold:** 2,000 chars ≈ 1 full page of dense text. Any PDF where pypdf extracts less than this is assumed scanned/image-only and routed to Docling.

**Package added:** `pypdf` installed in venv.

### Runbook created

New `runbook/` folder with:
- `RUNBOOK.md` — complete from-scratch setup guide for a new person
- `scripts/mount_icfo.sh` — remount ICFO network share after reboot
- `scripts/run_server_ingest.sh` — run/restart server document ingestion
- `scripts/run_repo_ingest.sh` — run/restart GitHub repo ingestion
- `scripts/clone_repos.sh` — clone all QNOE-group repos
- `scripts/health_check.sh` — check all components

---

## Phase 1 — Code Review + Bug Fixes (2026-06-18)

Full 4-sweep audit of all 16 Python files in `agent/`. 15 bugs found and fixed. See `BUG_REPORT.md` for full details.

**Key fixes deployed to DGX:**
- `agent/ingest/run_ingest.py` — `manifest_db` parameter added; `_delete_old_chunks` fixed for collection migration
- `agent/ingest/splitter.py` — temp file leak fixed; `DOCLING_OCR=1` env var flag added; SQLite conn leak fixed
- `agent/ingest/ocr_queue.py` — **new file**: OCR queue processor (see §9e in RUNBOOK)
- `agent/graph.py` — async fixes, duplicate system message removed
- `agent/retrieval.py` — switched to `AsyncQdrantClient`
- `agent/episodic.py` — async wrappers added
- `agent/teams.py` — async MSAL, proper datetime comparison
- `agent/main.py` — multi-turn memory fix, async REPL input

**Deploy procedure used** (qnoe-ai owns `/opt/qnoe-agent/agent/`):
```bash
scp file.py yzamir@10.3.8.21:/home/yzamir/file.py
sudo cp /home/yzamir/file.py /opt/qnoe-agent/agent/ingest/file.py
```

---

## Phase 1 — Confirmed Paper Docling Re-runs (2026-06-18)

Generated full-path lists from `SUSPECTED_PAPERS_REVIEW.md` using `generate_confirmed_lists.py`:
```bash
sed 's|/opt/qnoe-agent/runbook/...|/home/yzamir/...|' generate_confirmed_lists.py > /tmp/gen_lists.py
python3 /tmp/gen_lists.py
```

Results:
- W2 Manuscripts: 26 confirmed → all resolved → re-indexed with Docling (564 chunks, `/tmp/docling_rerun_w2.log`)
- W12 Theses & reports: 19 confirmed (6 missing on disk) → re-indexed with Docling (1,282 chunks, `/tmp/docling_rerun_w12.log`)
- W6 Papers & Books: 65 confirmed → all resolved → `/tmp/confirmed_papers_books.txt` → launched re-run (`/tmp/docling_w6.log`)

**1-chunk OCR run (W2+W12):** 1 file (`conductivity_nonlocal.pdf`) → re-indexed with `DOCLING_OCR=1`; still 1 chunk (genuinely short content).

---

## Phase 1 — OCR Queue (2026-06-18)

**Problem:** ~10k scanned PDFs in `/tmp/empty_pdfs.log` (< 200 chars from pypdf, never indexed). Log is append-only, still growing as W4/W10 workers run.

**Solution:** `agent/ingest/ocr_queue.py` — reads log, deduplicates, checks manifest to skip already-indexed files, re-indexes remainder with `DOCLING_OCR=1`. Safe to re-run any time; manifest is the "done" set.

**vLLM stopped** (user confirmed) → launched GPU OCR run with `DOCLING_DEVICE=cuda`:
```bash
setsid bash -c '
cd /opt/qnoe-agent
PYTHONPATH=/opt/qnoe-agent QDRANT_URL=http://localhost:6333 \
AGENT_DATA_DIR=/home/yzamir/qnoe_server_data \
EMBED_MODEL_PATH=/opt/qnoe-agent/models/nomic-embed \
DOCLING_DEVICE=cuda \
/opt/qnoe-agent/venv/bin/python -m agent.ingest.ocr_queue \
>> /tmp/ocr_gpu.log 2>&1; echo "Done: $?" >> /tmp/ocr_gpu.log
' &
```

Dry run confirmed: **10,027 pending files** at launch. Log: `/tmp/ocr_gpu.log`. ETA: 8–14 hours on GPU.

**Second run (2026-06-19):** Re-ran after W4/W10 workers completed. 10,872 files processed, 0 chunks indexed.

**Total OCR results:** 10,027 + 10,872 files processed across two runs → **1 chunk total**.

### Investigation — What are these "empty" PDFs? (2026-06-19)

Inspected 10 random samples. They are **not scanned documents** — they are **single-page matplotlib/instrument-generated plots** (5–300 KB each). pypdf extracts axis labels and tick values (47–121 chars), which is below the 200-char threshold → classified as "empty." Docling OCR finds nothing additional because there is no additional text — the content is visual (plot lines, data points, colorbars).

**Breakdown of 10,873 unique files:**

| Category | Count | % |
|---|---|---|
| Noise measurement plots | 4,691 | 43% |
| FTIR spectra | 2,551 | 24% |
| Other (paper figures, Keynote temps) | 2,363 | 22% |
| Transport plots | 914 | 8% |
| matplotlib test baselines (junk) | 354 | 3% |

**Decision:** Do not index. Axis labels alone are not useful for RAG. See `PHASE2_BACKLOG.md §B6` for a future VLM-based figure description approach.

---

### CIFS mount tuning (2026-06-18 15:30)

Remounted `/ICFO/groups/NOE` with aggressive caching to speed up concurrent ingestion workers:
```bash
sudo mount -o remount,cache=loose,rsize=131072,wsize=131072,actimeo=60 /ICFO/groups/NOE
```
Baseline file counts at 15:30 (for speed comparison at 16:30):
- W4 (Notebook): 15,793 indexed
- W10 (Projects): 12,126 indexed
- OCR GPU: 1 indexed (just started)

**Note:** these mount options are lost on reboot — add to `/etc/fstab` or remount script if needed.

---

### ICFO network mount dropped

DGX rebooted during session; ICFO mount at `/ICFO/groups/NOE` unmounted. Mount is configured via `pam_mount` (CIFS from `files.icfo.es`) — remounts automatically on interactive login but not on headless reboot. Manual remount requires ICFO domain password (pending IT). Server ingestion paused until mount is restored.

**Mount command** (once password known):
```bash
sudo mount -t cifs //files.icfo.es/groups/NOE /ICFO/groups/NOE \
  -o username=yzamir,uid=13180,gid=35001,domain=icfonet,vers=2.1
```

### PDF force-reindex completed (2026-06-17)

Run: `python -m agent.ingest.ingest_all --force-ext .pdf`

Result: 27 files re-indexed, 659 new chunks. Old stale chunks deleted before re-indexing (new `_delete_old_chunks()` logic confirmed working — DELETE + PUT pattern visible in log).

**Final Qdrant collection counts after all ingestion work:**
| Collection | Points |
|---|---|
| qed | 9,743 |
| photocurrent | 5,321 |
| group-wide | 6,195 |
| qsim | 2,749 |
| qtm | 253 |
| superconductivity | 231 |
| xchiral | 0 |

---

## Phase 1 — Orphan File Cleanup (2026-06-19)

### Problem

When files are deleted or moved on disk (server mount or GitHub repos), their chunks remain in Qdrant and their rows stay in the manifest DB. The nightly run only handled re-indexing changed files — it never detected removed files. This left stale chunks the agent could cite, pointing users to files that no longer exist.

### Solution

Added `sweep_orphans()` to `agent/ingest/run_ingest.py` and `task_orphan_cleanup()` to `agent/indexing/nightly_run.py`.

**Key design constraint — avoid false positives:** Files can be temporarily inaccessible (CIFS mount down, network errors, permission changes, server maintenance). A naive "file missing → delete chunks" would cause data loss on every mount hiccup.

**Approach:** Track "first seen missing" timestamps in a new `missing_files` SQLite table. Only delete from Qdrant after a file has been **continuously missing for 7+ days**. If the file reappears, the missing mark is cleared.

### New components

| Component | Location | Role |
|---|---|---|
| `missing_files` table | manifest DB (both repo + server) | Tracks `file_path`, `first_seen`, `last_checked` for inaccessible files |
| `_file_accessible()` | `run_ingest.py` | Safe check via `path.stat()` — catches file not found, permission denied, network timeout, stale NFS handles |
| `sweep_orphans()` | `run_ingest.py` | Core logic: scan manifest → check accessibility → track/delete orphans → return stats dict |
| `task_orphan_cleanup()` | `nightly_run.py` | Nightly task wrapper — runs sweep on repo manifest (always) + server manifest (only if mount is live) |

### `sweep_orphans()` logic

1. **Read all rows** from `index_manifest`
2. **For each file, check accessibility** via `_file_accessible()`
3. **If accessible** and in `missing_files` → remove from `missing_files` (recovered)
4. **If inaccessible** and not in `missing_files` → INSERT with `first_seen = now` (newly missing)
5. **If inaccessible** and already in `missing_files` → UPDATE `last_checked`
6. **If `first_seen` is 7+ days old** → delete Qdrant points + remove from `index_manifest` + remove from `missing_files`
7. **Returns:** `{"checked": N, "newly_missing": N, "still_missing": N, "recovered": N, "deleted": N}`

### Mount guard

`task_orphan_cleanup()` checks for `Group_Manual.txt` in the server root before sweeping the server manifest. If the mount is down, the entire server sweep is skipped (no server files get incorrectly marked as missing).

### Verification (all passed on DGX)

| Test | Command | Result |
|---|---|---|
| Smoke test | `sweep_orphans(repo_db, qdrant_url)` | 1,640 checked, 0 orphans |
| Task registration | `--dry-run --task orphan_cleanup` | Task found and queued |
| New missing | Insert fake row, sweep | `newly_missing: 1` |
| Grace period | Sweep again (< 7 days) | `still_missing: 1`, not deleted |
| Expiry | Backdate `first_seen` 8 days, sweep | `deleted: 1`, both tables cleaned |
| Recovery | Insert real path in `missing_files`, sweep | Row removed (file accessible) |

### Pending actions
- **Server ingestion** — once ICFO mount is restored (IT to fix or provide CIFS password)
- **Teams credentials** — still blocked on Azure app registration from IT

---

## Phase 1 — Cross-Encoder Reranker (2026-06-19)

### What was done

Added a cross-encoder reranking step to `agent/retrieval.py` between the Qdrant cosine-similarity fetch and the final top-5 selection. This is the single biggest retrieval quality improvement available — it lets the model judge whether a chunk actually *answers* the query, not just whether it's topically similar.

**Model:** `cross-encoder/ms-marco-MiniLM-L-6-v2` (~87 MB), saved to `/opt/qnoe-agent/models/cross-encoder-msmarco/`. Runs on CPU, doesn't conflict with vLLM GPU usage.

### New retrieval flow

```
1. Embed query (nomic-embed, CPU)                          ← _embed_query()
2. Query each collection top-20                            ← _query_one() via asyncio.gather
3. Merge all results, sort by cosine score, deduplicate    ← unchanged
4. Take top-20 candidates (pre-rerank pool)                ← NEW: widen pool
5. Cross-encoder rerank all candidates                     ← NEW: _rerank()
6. Take top-5 by cross-encoder score                       ← unchanged count
7. Fail if best cross-encoder score < RERANK_THRESHOLD     ← NEW threshold (0.5)
8. Anti-lost-in-middle reorder                             ← unchanged
```

### New code in `retrieval.py`

| Component | Purpose |
|---|---|
| `RERANK_MODEL_PATH` | Env-configurable path to cross-encoder model (default `/opt/qnoe-agent/models/cross-encoder-msmarco`) |
| `RERANK_POOL = 20` | Number of cosine-similarity candidates to pass to cross-encoder |
| `RERANK_THRESHOLD = 0.5` | Minimum cross-encoder score to consider a result relevant |
| `_load_reranker()` | `@lru_cache` loader — lazy-loads CrossEncoder on first query, stays in memory |
| `_rerank(query, chunks, top_k)` | Scores all query–chunk pairs, sorts by cross-encoder score, returns top-k |

The reranker runs in `loop.run_in_executor()` to keep the async interface non-blocking.

### Verification (all passed on DGX)

| Test | Result |
|---|---|
| Model files present | `config.json` + `model.safetensors` (87 MB) in `/opt/qnoe-agent/models/cross-encoder-msmarco/` |
| Smoke test (2 pairs) | Relevant: score 0.35, Irrelevant: score -11.37. Latency: 93ms cold |
| Integration test (real query) | `"band structure of graphene"` → 5 results, rerank scores 6.8–7.0 |
| Threshold test (nonsense) | `"xyzzy foobar blargh"` → 0 results (all below threshold) |
| End-to-end latency | 2.3s total (cold, includes loading both models); rerank adds ~50ms warm |

### Permission fix required

`sudo cp -r` preserves source permissions. `model.safetensors` was `chmod 600` (owner-only), so after copying as root it was unreadable by yzamir/qnoe-ai. Fixed with:
```bash
sudo chmod 644 /opt/qnoe-agent/models/cross-encoder-msmarco/model.safetensors
```

### Tuning note

`RERANK_THRESHOLD = 0.5` is a starting point. The ms-marco model outputs logits (not 0–1 probabilities) — typical relevant scores are 2–8, irrelevant are -5 to 0. Adjust after the RAG evaluation (TODO item #4).

---

## Phase 1 — QCoDeS Pipeline Refactor (2026-06-19)

### Problem

The QCoDeS `.db` file handling had five issues:

1. **Dual ingestion:** Both the generic pipeline (`splitter.py` → `group-wide`) and the dedicated scanner (`qcodes_scanner.py` → `qcodes-runs`) processed `.db` files, producing duplicate summary cards. The scanner tried to clean up `group-wide` duplicates, but only for that one collection — `.db` files ingested into `qed` or `photocurrent` by the repo pipeline were never cleaned.

2. **Full re-chunk on change:** The generic pipeline uses file-level SHA-256 hashing. When a DB gets a new run, the hash changes, all old chunks are deleted, and all runs are re-chunked. A DB with 500 runs gaining 1 new run re-embeds all 500. The scanner already handles this correctly (incremental — only new `run_id`s).

3. **No `Thumbs.db` filter in generic pipeline:** The scanner excluded `Thumbs.db` via `find`, but the generic pipeline matched `*.db` and tried to open every `Thumbs.db` as a QCoDeS database. Not harmful (table check returns `[]`), but wasteful I/O on CIFS.

4. **Full file read for hashing:** `qcodes_scanner.py` called `db_path.read_bytes()` to SHA-256 hash the entire file. QCoDeS databases can be hundreds of MB. Reading the full file over CIFS just for change detection is slow.

5. **No mount guard on QCoDeS scan:** `task_orphan_cleanup` checked for `Group_Manual.txt` before touching server files, but `task_scan_qcodes` didn't — if the CIFS mount was down, `find` would hang or return nothing.

6. **Sync Qdrant client:** `qcodes_scanner.py` used the synchronous `QdrantClient` while the rest of the codebase uses `AsyncQdrantClient`.

### Changes made

| File | Change |
|---|---|
| `agent/ingest/run_ingest.py` | Removed `.db` from `SUPPORTED_EXTENSIONS` |
| `agent/ingest/splitter.py` | Removed `_chunk_qcodes_db()` function and `.db` case from `chunk_file()` |
| `agent/ingest/qcodes_scanner.py` | Removed `_delete_qcodes_from_group_wide()` (no longer needed); replaced full-file SHA-256 with `stat()` fingerprint (size + mtime); converted to `AsyncQdrantClient`; added `timeout=300` to `find` subprocess |
| `agent/indexing/nightly_run.py` | Added `import asyncio`; added mount guard (`Group_Manual.txt`) to `task_scan_qcodes`; wrapped `scan_qcodes()` call with `asyncio.run()` |

### Design after refactor

`.db` files are now handled **exclusively** by `qcodes_scanner.py`. The generic ingestion pipeline (`splitter.py` / `run_ingest.py`) ignores them entirely.

```
Generic pipeline (nightly):
  Repos + Server folders → .py .ipynb .md .pdf .pptx .docx → team collections

QCoDeS scanner (nightly, after generic):
  Repos + ALL of /ICFO/groups/NOE → *.db → qcodes-runs collection
  - Incremental: only new run_ids are embedded
  - Fingerprint: stat() size+mtime, not full file read
  - Mount guard: skips server if CIFS is down
```

### Deployment

All 4 files deployed to DGX via `scp` + `sudo cp` (2026-06-19).

`qcodes-runs` collection exists with **74,760 points** from **57 unique QCoDeS DBs**. Notebook folder scan still in progress (see below).

### QCoDeS scan results (2026-06-19 EOD)

| Metric | Value |
|---|---|
| `qcodes-runs` collection | **74,760 points**, status: green |
| Unique DBs in `qcodes_db_hashes` | 57 |
| Runs in `qcodes_registry` | 74,760 |
| Notebook folder | `find *.db` running 2h+ — CIFS bottleneck |

**Key findings:**
- Most `.db` files in cloned repos are Git LFS stubs (130-133 bytes), not real databases. Only 3 of 122 repo `.db` files were real.
- Real QCoDeS databases are on the CIFS server — especially `QCoDeS/`, `Setups/`, `Notebook/`, `Personal/` folders.
- Personal folder alone: 22 DBs, 3,719 runs.

### CIFS `find` performance problem

Notebook folder `find *.db` ran for 2+ hours without completing. This is the same bottleneck affecting `task_index_server` and `task_scan_qcodes` nightly. The folder has 32k+ files over SMB 3.1.1.

**Permanent fix planned:** SMB3 `CIFS_IOC_NOTIFY` watcher daemon — see `WATCHER_PLAN.md`. Replaces nightly `find`-based scans with continuous change detection via kernel ioctl (`0x4005cf09`). Verified working on this mount (kernel 6.17, SMB 3.1.1). Plan pending user review.

### Background worker (check next session)

Notebook QCoDeS scanner (PID 2134481) still in `find` phase at session end. Check:
```bash
ps aux | grep qcodes_scanner | grep -v grep
sqlite3 /home/yzamir/qnoe_server_data/episodic.db "SELECT COUNT(*) FROM qcodes_db_hashes"
curl -s http://localhost:6333/collections/qcodes-runs | python3 -c 'import sys,json; print(json.load(sys.stdin)["result"]["points_count"])'
```

---

## When IT Grants Sudo Access

Do these in order:

### A. Install missing system packages
```bash
sudo apt install python3.12-dev
```
This unblocks vLLM serving (Step 4). After installing, re-run:
```bash
vllm serve ~/qnoe-agent/models/hermes-3-70b-awq --host 127.0.0.1 --port 8000 --quantization awq_marlin --max-model-len 32768
```

### B. Migrate from `~/qnoe-agent/` to `/opt/qnoe-agent/`
```bash
sudo mkdir -p /opt/qnoe-agent
# IT created qnoe-ai account on 2026-06-09 — no need to useradd
sudo chown qnoe-ai /opt/qnoe-agent
```
Then copy everything over:
```bash
# Copy models (large — will take time on local NVMe, fast)
sudo cp -r ~/qnoe-agent/models /opt/qnoe-agent/models

# Copy Qdrant data
sudo cp -r ~/qnoe-agent/qdrant_data /opt/qnoe-agent/qdrant_data

# Copy Qdrant binary
sudo cp -r ~/qnoe-agent/bin /opt/qnoe-agent/bin

# Copy SQLite DB
sudo cp -r ~/qnoe-agent/memory /opt/qnoe-agent/memory

# Copy secrets
sudo cp -r ~/qnoe-agent/secrets /opt/qnoe-agent/secrets
sudo chmod 600 /opt/qnoe-agent/secrets/github_pat

# Copy agent code (once written)
sudo cp -r ~/qnoe-agent/agent /opt/qnoe-agent/agent

# Recreate venv at new path (do NOT copy — venvs are path-sensitive)
sudo -u qnoe-ai uv venv /opt/qnoe-agent/venv --python 3.12
sudo -u qnoe-ai /opt/qnoe-agent/venv/bin/uv pip install vllm sentence-transformers qdrant-client langgraph
```

### C. Replace GitHub dev PAT with org-scoped PAT
Once `qnoe-ai` GitHub account is created:
```bash
sudo -u qnoe-ai bash -c 'echo "NEW_ORG_PAT" > /opt/qnoe-agent/secrets/github_pat && chmod 600 /opt/qnoe-agent/secrets/github_pat'
```
Also update git config:
```bash
sudo -u qnoe-ai git config --global user.name "QNOE Agent"
sudo -u qnoe-ai git config --global user.email "qnoe-ai@icfo.net"  # confirm email with IT
```

### D. Set up OpenShell sandbox environment (Step 11)

**Decision (2026-06-09):** manual `.bashrc` hardening superseded by NVIDIA OpenShell.
See `DGX_SETUP.md §11` and `OPENSHELL_DESIGN_PROPOSAL.md` for full spec.

Steps:
```bash
# Docker already configured (2026-06-09) — yzamir and qnoe-ai in docker group

# Install OpenShell CLI via uv (installs to /home/qnoe-ai/.local/bin/openshell)
sudo -u qnoe-ai uv tool install -U openshell
# PATH fix needed:
sudo -u qnoe-ai bash -c 'echo "export PATH=\"/home/qnoe-ai/.local/bin:\$PATH\"" >> /home/qnoe-ai/.profile'

# Install gateway binary (separate deb — uv only installs the CLI)
curl -LsSf -o /tmp/openshell.deb "https://github.com/NVIDIA/OpenShell/releases/download/v0.0.59/openshell_0.0.59-1_arm64.deb"
sudo apt install -y /tmp/openshell.deb
# → installs /usr/bin/openshell-gateway

# Enable lingering so qnoe-ai services persist without login
sudo loginctl enable-linger qnoe-ai

# Start gateway (no TLS, Docker driver, localhost only)
sudo -u qnoe-ai bash -c 'setsid /usr/bin/openshell-gateway --disable-tls --drivers docker > /opt/qnoe-agent/logs/gateway.log 2>&1 &'
sleep 3

# Register gateway with CLI
sudo -u qnoe-ai bash -c 'source /home/qnoe-ai/.profile && openshell gateway add http://127.0.0.1:17670'
sudo -u qnoe-ai bash -c 'source /home/qnoe-ai/.profile && openshell status'   # → Connected

# Register vLLM inference provider
HOST_IP=$(hostname -I | awk '{print $1}')
sudo -u qnoe-ai openshell provider create \
  --name local-vllm --type openai \
  --credential OPENAI_API_KEY=none \
  --config OPENAI_BASE_URL=http://${HOST_IP}:8000/v1
sudo -u qnoe-ai openshell inference set --provider local-vllm --model hermes-3-70b-awq

# Write Dockerfile and sandbox-policy.yaml (see DGX_SETUP.md §11.2 and §11.3)
# Confirm Docker bridge IP for Qdrant policy:
ip addr show docker0 | grep inet
```

### E. Set up systemd services + nightly cron (Step 12)
```bash
# Write service files (see DGX_SETUP.md §12)
sudo cp /opt/qnoe-agent/config/openshell-gateway.service /etc/systemd/system/
sudo cp /opt/qnoe-agent/config/qnoe-agent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable openshell-gateway qnoe-agent
sudo systemctl start openshell-gateway
sudo systemctl start qnoe-agent
```
See `DGX_SETUP.md §12` for full service file specs and cron job.

### F. Run validation checklist
After all of the above, run through the full validation checklist at the bottom of `DGX_SETUP.md` to confirm Phase 0 is complete.

---

## Phase 1 — Agent Service Deployment (2026-06-24)

### G. vLLM restarted, TODO updated

- vLLM restarted via `sudo systemctl start vllm` — confirmed serving Hermes 3 70B AWQ at `localhost:8000`
- TODO.md updated: marked OCR W6, ingestion pipeline, and cron jobs as complete

### H. OpenShell sandbox mount failure — root cause and migration

**Problem:** `qnoe-agent.service` crash-looped on startup. `journalctl` showed "No such file or directory" for `/opt/qnoe-agent/venv/bin/python`.

**Root cause (two issues):**

1. **Python symlink mismatch:** The venv's `python` symlink points to `/usr/bin/python3.12` (the host path), but the `python:3.12-slim` Docker image has Python at `/usr/local/bin/python3.12`. Fixed by adding to Dockerfile:
   ```dockerfile
   RUN ln -s /usr/local/bin/python3.12 /usr/bin/python3.12
   ```

2. **OpenShell v0.0.59 does not mount user volumes:** The `--driver-config-json` flag for Docker mounts is marked "Experimental" in the OpenShell binary and is silently ignored. `docker inspect` on a running OpenShell sandbox confirmed only OpenShell's own internal mounts (supervisor binary, TLS certs, JWT) were present — no user-specified mounts. This means `/opt/qnoe-agent/` was never bind-mounted into the container, so the venv, agent code, models, etc. were all missing.

   Investigation included: Docker inspect, binary string analysis of `openshell-gateway`, web research (NVIDIA docs, GitHub, release notes), and verbose `-vvv` mode testing. Conclusion: mount passthrough genuinely not implemented in v0.0.59 for the Docker driver.

**Solution:** Replace OpenShell sandbox with plain `docker run` using native `-v` bind mounts.

New start script: `/opt/qnoe-agent/scripts/start_agent.sh`
```bash
#!/bin/bash
set -e
docker rm -f qnoe-agent 2>/dev/null || true
exec docker run --rm \
  --name qnoe-agent \
  --network host \
  -v /opt/qnoe-agent/venv:/opt/qnoe-agent/venv:ro \
  -v /opt/qnoe-agent/agent:/opt/qnoe-agent/agent:ro \
  -v /opt/qnoe-agent/config:/opt/qnoe-agent/config:ro \
  -v /opt/qnoe-agent/secrets:/opt/qnoe-agent/secrets:ro \
  -v /opt/qnoe-agent/models:/opt/qnoe-agent/models:ro \
  -v /opt/qnoe-agent/memory:/opt/qnoe-agent/memory \
  -v /opt/qnoe-agent/logs:/opt/qnoe-agent/logs \
  -v /opt/qnoe-agent/skills:/opt/qnoe-agent/skills \
  -v /opt/qnoe-agent/repos:/opt/qnoe-agent/repos:ro \
  -v /ICFO/groups/NOE:/ICFO/groups/NOE:ro \
  -e VLLM_BASE_URL=http://localhost:8000/v1 \
  -e QDRANT_URL=http://localhost:6333 \
  -e AGENT_DATA_DIR=/opt/qnoe-agent/memory \
  -e EMBED_MODEL_PATH=/opt/qnoe-agent/models/nomic-embed \
  -e TRANSFORMERS_OFFLINE=1 \
  -e HF_HUB_OFFLINE=1 \
  -e HF_DATASETS_OFFLINE=1 \
  qnoe-agent:latest \
  /opt/qnoe-agent/venv/bin/python -m agent.main
```

**Systemd changes:** Remove `openshell-gateway.service` dependency from `qnoe-agent.service`.

### I. Policy enforcement gap analysis

| Capability | OpenShell sandbox | Plain Docker run | Status |
|---|---|---|---|
| Filesystem read-only | sandbox-policy.yaml | `-v path:path:ro` flags | **Preserved** |
| Filesystem read-write | sandbox-policy.yaml | `-v path:path` (default rw) | **Preserved** |
| Network L7 inspection | OpenShell proxy | Not available | **Lost** |
| Credential scoping | OpenShell credential store | Env vars in container | **Lost** |
| Sandbox JWT auth | OpenShell gateway | Not available | **Lost** |
| Process isolation | Docker container | Docker container | **Preserved** |

**Verdict:** Acceptable for Phase 1 (T0/T1 read-only). Phase 2 (T2+ write access) needs revisiting — options include upgrading OpenShell, using Docker AppArmor/seccomp profiles, or implementing approval gates in agent code.

### J. Agent service deployed (2026-06-30)

The following commands need to be run on the DGX to complete the migration:

```bash
# 1. Stop current (crash-looping) service
sudo systemctl stop qnoe-agent

# 2. Clean up OpenShell sandboxes
sudo -u qnoe-ai openshell sandbox delete qnoe-agent 2>/dev/null
sudo -u qnoe-ai openshell sandbox delete test-agent 2>/dev/null
sudo -u qnoe-ai openshell sandbox delete test-mount 2>/dev/null

# 3. Deploy new start script
sudo cp /tmp/start_agent.sh /opt/qnoe-agent/scripts/start_agent.sh
sudo chmod +x /opt/qnoe-agent/scripts/start_agent.sh

# 4. Update systemd unit — remove openshell-gateway dependency
sudo sed -i 's/After=network.target docker.service openshell-gateway.service vllm.service/After=network.target docker.service vllm.service/' /etc/systemd/system/qnoe-agent.service
sudo sed -i 's/Requires=docker.service openshell-gateway.service vllm.service/Requires=docker.service vllm.service/' /etc/systemd/system/qnoe-agent.service
sudo systemctl daemon-reload

# 5. Start agent
sudo systemctl start qnoe-agent

# 6. Verify
sudo journalctl -u qnoe-agent -n 20 --no-pager
```

### Issues fixed during deployment

1. **Missing PYTHONPATH** — Container couldn't find `agent` module. Fixed: `-e PYTHONPATH=/opt/qnoe-agent`
2. **SQLite readonly** — Container ran as `sandbox` (uid 1000660000), couldn't write to `qnoe-ai`-owned dirs. Fixed: `--user 1001:1001`
3. **PyTorch getpwuid** — No passwd entry for uid 1001 in container. Fixed: generate `/etc/passwd` with qnoe-ai entry, mount read-only
4. **Teams env file permissions** — `teams.env` owned by root:root 600, service runs as qnoe-ai. Fixed: `chown qnoe-ai:qnoe-ai`
5. **Log path** — `agent.log` tried to write to `/opt/qnoe-agent/memory/`. Fixed: `AGENT_LOG_DIR=/opt/qnoe-agent/logs`

### Final status

Agent running in Docker container as uid 1001 (qnoe-ai), Teams polling active, logs at `/opt/qnoe-agent/logs/agent.log`.

---

## Watcher Test Results (2026-06-30)

Full test of `qnoe-watcher.service` — all 14 acceptance criteria validated.

| # | Test | Result | Notes |
|---|------|--------|-------|
| 1 | New file detection | **PASS** | test.pptx appeared in queue as `change_type=new` |
| 2 | Modified file detection | **PASS** | `change_type=modified` after editing existing file |
| 3 | Deleted file detection | **PASS** | `change_type=deleted` after removing file |
| 4 | Stability marking | **PASS** | `stable_at` set by StabilityChecker after 30 min |
| 5 | Queue processing | **PASS** | 4 entries processed, 2 files ingested into Qdrant `group-wide` |
| 6 | Double-run idempotency | **PASS** | Second run: "no stable entries to process" |
| 7 | Active .db stays pending | **PASS** | `stable_at = NULL` while background writes ongoing |
| 8 | SubfolderManager spawn | **PASS** | "Started watcher: Projects/watcher_test_subfolder" |
| 9 | SubfolderManager removal | **PASS** | Bug found & fixed (see below) |
| 10 | Exclusion enforcement | **PASS** | 0 files from 6 excluded folders in cache/queue |
| 11 | Mount loss handling | **PASS** | Bug found & fixed (see below) |
| 12 | Remount recovery | **PASS** | "Mount restored — triggering cache rebuild" |
| 13 | CacheRebuilder rebuild | **PASS** | 182 folders rebuilt, 0 duplicates in file_cache |
| 14 | Oversized file skip | **PASS** | "Oversized (55 MB)" logged, not ingested |

### Bugs found and fixed

1. **SubfolderManager orphaned threads (smb_watcher.py):** When a subfolder was deleted, `_sync_children()` removed the watcher from its dict but didn't stop the FolderWatcher thread — it kept retrying every 60s. **Fix:** Each child FolderWatcher now gets its own `threading.Event`; SubfolderManager sets it on removal. Also propagates stop to all children on daemon shutdown.

2. **MountMonitor didn't detect lazy unmount (smb_watcher.py):** `_check_mount()` used `os.stat()`, which succeeds on stale mount points after `umount -l`. **Fix:** Changed to `os.path.ismount()`, which checks device IDs.

3. **`.txt` files removed from watcher (watcher.yaml):** 48,799 `.txt` files in cache were raw measurement data (voltage sweeps, temperature scans, etc.), not documents. Removed `.txt` from `supported_extensions`. Cache dropped from 83,741 → 37,346 files.

### Performance notes

- CacheRebuilder full scan: ~44 minutes across 182 folders over CIFS
- Single large folder (`Manuscripts`, 5,450 files): ~2 minutes via `find` over CIFS
- `process_change_queue`: <3s for 4 entries (including embedding model load)

---

## Phase 1 — Agent Teams Integration (2026-06-30)

### K. Teams connector fixes

Multiple issues found and fixed during first live deployment:

| # | Issue | Root cause | Fix |
|---|-------|-----------|-----|
| 1 | MSAL `ValueError: offline_access` | MSAL considers `offline_access` reserved, adds it automatically; passing it explicitly causes ValueError | Removed `"offline_access"` from `SCOPES` in `teams.py` |
| 2 | `'coroutine' object is not iterable` | Deployed `graph.py` was an older version missing `await` on `retrieve()` and `get_episodic_context()` calls | Redeployed current local versions of `graph.py`, `episodic.py`, `teams.py`, `main.py` |
| 3 | Graph API `$filter` returns 0 chats | `$filter=chatType eq 'oneOnOne'` returned empty results on Graph API | Changed `_refresh_chat_list()` to fetch all chats with `$select=id,chatType&$top=50` and filter client-side |
| 4 | nomic-embed HuggingFace connection failure | Model has custom code (`NomicBertModel`) with `auto_map` referencing `nomic-ai/nomic-bert-2048` on HuggingFace. Custom `.py` files were not in the local model directory, so `trust_remote_code=True` triggered a download attempt that failed in the container | Copied `configuration_hf_nomic_bert.py`, `modeling_hf_nomic_bert.py`, `__init__.py` from host HF cache into `/opt/qnoe-agent/models/nomic-embed/`. Updated `config.json` `auto_map` entries to use local paths (stripped `nomic-ai/nomic-bert-2048--` prefix) |

### L. First successful Teams response

After all fixes, the agent successfully:
1. Authenticates with Teams as "QNOE Agent" (id `aa2b5ee6`)
2. Discovers DM chats (oneOnOne filter)
3. Polls for new messages (3s active / 10s idle)
4. Routes through LangGraph → vLLM (Hermes 3 70B)
5. Retrieves RAG context from Qdrant (nomic-embed + cross-encoder reranker)
6. Sends reply via Graph API

**Response time breakdown (warm, follow-up message):**

| Step | Duration |
|---|---|
| Poll interval (idle) | ~10s |
| Embed query (nomic-embed, CPU) | <0.5s |
| Qdrant queries (3 collections) | <1s |
| Cross-encoder rerank | <1s |
| vLLM inference (70B model) | ~10s |
| **Total (idle → reply)** | **~15–20s** |

First message after cold start adds ~2s for model loading (nomic-embed + cross-encoder).

**Idle poll interval reduced** from 30s → 10s in `teams.py` for better responsiveness.

### M. File access tools (`agent/tools.py`)

The agent had no ability to read files from the lab server or cloned repos — it could only answer from RAG context. Added two tools callable by the LLM via vLLM tool-calling:

| Tool | Purpose |
|---|---|
| `read_file` | Read file contents (max 50 KB) |
| `list_directory` | List directory entries (max 200) |

**Security:**
- Path validation: only `/ICFO/groups/NOE/` and `/opt/qnoe-agent/repos/` allowed
- Path traversal (`..`) blocked
- File size cap: 50 KB (rejects with guidance to ask user for specific section)
- Both paths mounted read-only in container

**Implementation:**
- `agent/tools.py` — tool functions + OpenAI-format tool definitions + `execute_tool_call()` dispatcher
- `agent/llm.py` — added `chat_with_tools()`: passes `tools` schema to vLLM, parses `tool_calls` in response, executes tools in executor, feeds results back, loops up to 5 rounds
- `agent/graph.py` — `_subagent_respond()` now uses `chat_with_tools()` instead of `chat()`
- `agent/prompts.py` — added FILE ACCESS block to both orchestrator and sub-agent prompts

### N. QCoDeS scan gap — missing measurements

**Problem:** User reported QTM measurements from `Setups/L110 QTM/Measurement/` were not in the knowledge base. Investigation found 4 of 7 `.db` files in that folder were never indexed.

**Root cause (two issues):**

1. **`find` timeout too short:** `qcodes_scanner.py` used `subprocess.run(..., timeout=300)` (5 minutes) when scanning the entire `/ICFO/groups/NOE` root. A full `find *.db` over CIFS takes 2+ hours. The `find` was killed mid-traversal and only returned DBs discovered before the timeout. The 3 indexed DBs (`2026.04`, `2026.05`, `2026.06_Tip6`) happened to be in folders `find` reached first; `2026.02_Tip5Sample5_qcodes/` was simply not traversed before timeout.

   **Fix:** Increased timeout from 300s → 7200s (2 hours) in `_find_db_files()`.

2. **Watcher not monitoring `Setups/` folder:** The watcher daemon config (`watcher.yaml`) only had `Projects`, `Notebook`, `Notebooks` in `watch_subfolder_level`. The `Setups/` folder (37 subfolders including `L110 QTM`) was not being watched, so new `.db` files there never entered the change queue.

   **Fix:** Added `Setups`, `Personal`, and `Fabrication` to `watch_subfolder_level` in `watcher.yaml`.

**Full rescan:** Kicked off background QCoDeS rescan of entire server with 2h timeout. Already-indexed unchanged DBs are skipped via hash table. Running at `/opt/qnoe-agent/logs/qcodes_rescan.log`.

### Files changed this session

| File | Change |
|---|---|
| `agent/teams.py` | Removed `offline_access` scope; client-side chat filter; idle poll 30→10s |
| `agent/graph.py` | Use `chat_with_tools()` in `_subagent_respond()` |
| `agent/llm.py` | Added `chat_with_tools()` with tool-call loop (max 5 rounds) |
| `agent/tools.py` | **New** — `read_file`, `list_directory` tools with path validation |
| `agent/prompts.py` | Added FILE ACCESS block to orchestrator + sub-agent prompts |
| `agent/main.py` | Log path uses `AGENT_LOG_DIR` instead of `AGENT_DATA_DIR` |
| `agent/ingest/qcodes_scanner.py` | `find` timeout 300s → 7200s |
| `config/watcher.yaml` | Added `Setups`, `Personal`, `Fabrication` to `watch_subfolder_level` |
| `/opt/qnoe-agent/models/nomic-embed/` | Added custom code `.py` files + updated `config.json` `auto_map` |

### O. Unified exclusion list (`agent/ingest/excluded.py`)

The `find` commands in `qcodes_scanner.py`, `run_ingest.py`, and `nightly_run.py` each had their own exclusion logic (`FIND_PRUNE_DIRS` env var, `EXCLUDE_SUBFOLDERS` constant). Unified into a single module that reads `exclude_subfolders` from `watcher.yaml`.

| File | Change |
|---|---|
| `agent/ingest/excluded.py` | **New** — `get_excluded_paths()` and `find_prune_args()`, reads from `watcher.yaml` |
| `agent/ingest/qcodes_scanner.py` | Uses `find_prune_args()` in `_find_db_files()`; removed `find` timeout entirely |
| `agent/ingest/run_ingest.py` | Uses `find_prune_args()` in `_find_files()`; removed `FIND_PRUNE_DIRS` env var logic |
| `agent/indexing/nightly_run.py` | Removed `EXCLUDE_SUBFOLDERS` constant and `FIND_PRUNE_DIRS` env var setting |

Single source of truth: `config/watcher.yaml` → `exclude_subfolders`. Any folder added there is automatically excluded from the watcher, QCoDeS scanner, document ingestion, and nightly jobs.

### P. QCoDeS full rescan results (2026-06-30)

Full rescan completed after ~2 hours (17:02–19:00). The `find` phase over CIFS took ~1h50m; indexing took ~10 minutes.

| Metric | Before | After |
|---|---|---|
| Unique QCoDeS DBs | 57 | 75 (+18) |
| Total runs in `qcodes-runs` | 74,760 | 75,477 (+717) |

New databases indexed include:
- `Setups/L110 QTM/Measurement/` — 4 previously missing DBs (2026.02, 2026.06 Tip8 sessions)
- Various cSNOM/BSCCO/NbN databases from `Notebook/` and `Personal/` folders
- Previously missed due to 5-minute `find` timeout (now removed)

### Current DGX state

| Component | Status |
|---|---|
| vLLM | RUNNING — Hermes 3 70B AWQ at localhost:8000 |
| Qdrant | RUNNING — 8 collections, port 6333, 75,477 QCoDeS runs |
| qnoe-agent | RUNNING — Teams polling, file tools, RAG (LangGraph, will be replaced by Hermes Agent) |
| qnoe-watcher | RUNNING — watches Setups/, Personal/, Fabrication/ + original folders |
| QCoDeS rescan | DONE — 75 DBs, 717 new runs indexed |

---

## Migration: LangGraph to Hermes Agent (2026-06-30)

**Decision:** Migrate the agent conversation layer from custom LangGraph to Nous Research's Hermes Agent (v0.17.0). See `HERMES_AGENT_COMPARISON.md` for rationale and `MIGRATION_PLAN.md` for the full plan.

**Why:** The LangGraph agent lacks persistent memory (cross-session facts), self-improving skills, and sophisticated context management. Hermes Agent has all of these built-in plus 90+ tools, a gateway messaging system, and active community maintenance. The infrastructure (vLLM, Qdrant, watcher, ingestion, nightly indexing) stays untouched — only the conversation layer changes.

### Q. Hermes Agent installed (Phase M1 — DONE)

| Step | Status | Notes |
|---|---|---|
| M1.1 Install | DONE | `hermes-agent==0.17.0` in `/opt/qnoe-agent/hermes-venv/` (separate venv — openai version conflict with vLLM venv) |
| M1.2 Directory structure | DONE | `/opt/qnoe-agent/hermes/` with profiles, skills, plugins, cron, sessions, logs |
| M1.3 Configure for vLLM | DONE | `config.yaml` with `custom_providers` pointing to `localhost:8000/v1` |
| M1.4 Verify operation | DONE | Hermes calls vLLM successfully, gets responses |
| M1.5 Path security | DONE | Built-in file tools work; write_approval off (fine for T0/T1) |

**Patches applied:**
- `MINIMUM_CONTEXT_LENGTH` in `agent/model_metadata.py`: 64,000 → 16,000 (our model has 32K context; Hermes requires 64K minimum by default)

**Known issues:**
- Hermes 3 70B with full tool schema (42KB, 17 tools) tends to invoke tools instead of answering directly. Will need system prompt tuning.
- CLI `-z` mode doesn't load profile-specific SOUL.md (loads global SOUL.md instead). Gateway mode does use profiles correctly via `set_hermes_home_override`.

### R. Profiles created (Phase M2 — DONE)

| Profile | SOUL.md | MEMORY.md | Notes |
|---|---|---|---|
| qnoe-orchestrator | DONE | DONE | Converted from `ORCHESTRATOR_PROMPT` in `prompts.py` |
| qnoe-qtm | DONE | DONE | Converted from QTM sub-agent prompt; includes measurement data paths |
| qnoe-photocurrent | DONE | DONE | Converted from Photocurrent sub-agent prompt |

All 3 profiles visible via `hermes profile list`. Files at `/opt/qnoe-agent/hermes/profiles/`.

### Migration phases remaining

| Phase | Task | Status |
|---|---|---|
| M3 | RAG memory provider plugin (Qdrant retrieval) | **DONE** |
| M4 | QCoDeS tool | **DONE** |
| M5 | Teams polling adapter plugin | **DONE** (e2e test in M7) |
| M6 | Multi-agent routing via delegation | **DONE** |
| M7 | Deployment and cutover | **DONE** |
| M8 | Cleanup and documentation | TODO |

See `MIGRATION_PLAN.md` for detailed steps within each phase.

---

## S. Phase M3 — RAG Memory Provider Plugin (2026-07-01)

**Goal:** Integrate Qdrant RAG retrieval as a Hermes memory provider plugin.

### What was done

1. Created `plugins/qnoe_rag/` in `$HERMES_HOME/plugins/` (flat, not nested under `memory/`)
   - Hermes user plugin scanner checks direct children of `$HERMES_HOME/plugins/`
   - Memory plugins detected by `_is_memory_provider_dir()` (checks for `MemoryProvider` in `__init__.py`)
2. Implemented `QnoeRagProvider(MemoryProvider)` with:
   - `prefetch()` — synchronous fallback + background thread result consumption
   - `queue_prefetch()` — background thread for next-turn retrieval
   - `system_prompt_block()` — injects active collection list
   - `get_tool_schemas()` — exposes `rag_search` tool
   - `handle_tool_call()` — explicit search with optional collection filter
3. Profile→collection routing: `PROFILE_COLLECTIONS` map keyed by `agent_identity`
4. Configured `memory.provider: qnoe_rag` in config.yaml
5. Installed `einops` in hermes-venv (required by nomic-embed custom code)

### Key facts
- Plugin path: `/opt/qnoe-agent/hermes/plugins/qnoe_rag/`
- Plugin type: memory provider (exclusive — has its own activation path, not in `plugins.enabled`)
- Models loaded on CPU: nomic-embed + cross-encoder (same as existing retrieval.py)
- Retrieval: embed → Qdrant top-20/collection → deduplicate → cross-encoder rerank → top-5 → anti-lost-in-middle

### Test results
- `hermes memory status`: provider active, available
- Plugin discovery: found among 9 providers (8 bundled + 1 user)
- Tool schemas: `rag_search` registered
- Prefetch: returns relevant chunks (scores 6-7+) for "gate voltage sweep graphene"
- MemoryManager integration: system prompt block + prefetch both working through AIAgent

---

## T. Phase M4 — QCoDeS Tool Plugin (2026-07-01)

**Goal:** Expose QCoDeS measurement registry as a searchable Hermes tool.

### What was done

1. Created `plugins/qnoe_qcodes/` as a standalone plugin (requires `plugins.enabled`)
2. Implemented `qcodes_search` tool with filters: sample, experiment, date_from, date_to, free-text query, limit
3. Added `qnoe-lab` toolset + `qnoe_qcodes` to `plugins.enabled` in config.yaml

### Fixes during implementation
- DB filename: `episodic.db` not `manifest.db` (QCoDeS scanner writes to same DB as ingestion)
- Default `AGENT_DATA_DIR`: `/home/yzamir/qnoe_server_data` (where the 75,994-run registry lives)
- Timestamps: stored as Unix epoch in TEXT column — added `_epoch_to_iso()` / `_iso_to_epoch()` converters

### Test results
- Plugin discovery: enabled=True via `plugins.enabled`
- Sample search: `SLG09` → 3 results (Phototransport / SLG09_C4)
- Date range: `2025-06-01..2025-06-30` → 3 results (Cernox)
- Free text: `volt` → matches parameter names
- Total registry: 75,994 runs

---

## U. Phase M5 — Teams Polling Adapter Plugin (2026-07-01)

**Goal:** Replace the custom `teams.py` connector with a Hermes platform adapter plugin.

### What was done

1. Created `plugins/teams_polling/` as a platform plugin (kind: platform)
2. Implemented `TeamsPollingAdapter(BasePlatformAdapter)` with:
   - MSAL ROPC auth (same as existing connector)
   - Graph API polling loop (DM chats, 3s active / 10s idle)
   - `connect()` — bootstrap (auth + chat enum) then start poll task
   - `disconnect()` — cancel poll task, close HTTP session
   - `send()` — POST to Graph API chat messages endpoint
   - `get_chat_info()` — GET chat metadata
   - `handle_message()` — inherited from base, creates MessageEvent from poll results
3. Registered via `ctx.register_platform()` — dynamic Platform enum member
4. Added to `plugins.enabled` and `gateway.platforms.teams_polling` in config.yaml
5. HTML stripping for Teams message body content

### Key facts
- Plugin path: `/opt/qnoe-agent/hermes/plugins/teams_polling/`
- Platform name: `teams_polling` (dynamic Platform enum via `_missing_()`)
- Auth: MSAL ROPC via env vars (TEAMS_TENANT_ID, TEAMS_CLIENT_ID, TEAMS_USERNAME, TEAMS_PASSWORD)
- Env vars in `/opt/qnoe-agent/secrets/teams.env` (chmod 600, owned by qnoe-ai)
- Polling: creates `MessageEvent` with `SessionSource` and calls `self.handle_message(event)`
- Session keying: Hermes gateway handles this via `build_session_key()` on the SessionSource

### Test results
- Plugin discovery: `teams_polling: enabled=True, error=None`
- Platform registry: `teams_polling: label=Teams (Polling), source=plugin`
- Adapter instantiation: `TeamsPollingAdapter` created with correct poll intervals
- check_fn: returns True when TEAMS_* env vars are set
- End-to-end test deferred to M7 (requires running as qnoe-ai with teams.env)

---

## V. Phase M6 — Multi-Agent Routing (2026-07-01)

**Goal:** Configure orchestrator delegation to sub-team agents and verify RAG-based routing.

### Key findings

- `delegate_task` spawns fresh child agents with isolated contexts — it does NOT load profile SOUL.md files
- Sub-team context must be passed explicitly via the `context` parameter
- `hermes-cli` toolset already includes `delegate_task`; subagents auto-stripped of `delegate_task`, `memory`, `clarify`, `execute_code`, `send_message`
- Orchestrator searches all 8 Qdrant collections; `rag_search` tool `collection` parameter enables targeted queries
- For Phase 1 (2 sub-teams), orchestrator handles most queries directly; delegation reserved for complex multi-step tasks

### What was done

1. Added `delegation` config block to `config.yaml`:
   - `max_iterations: 25` (conservative for 32K context)
   - `max_concurrent_children: 2`, `max_async_children: 1`
   - `max_spawn_depth: 1` (flat: orchestrator → leaf only)
   - `subagent_auto_approve: false` (safe default)

2. Updated orchestrator SOUL.md:
   - Routing rules updated: answer directly for simple queries, delegate for complex/multi-step tasks
   - Added QTM and Photocurrent sub-team context blocks (for passing to `delegate_task`)
   - Added delegation examples (single-task and parallel cross-team)

3. Verified delegation config loads correctly from YAML

### Test results

- Delegation config: all 8 keys loaded correctly from config.yaml
- `delegate_task` available in `hermes-cli` toolset (48 tools total)
- Subagent blocked tools: `clarify`, `delegate_task`, `execute_code`, `memory`, `send_message`
- RAG targeted query (`collection="qtm"`): 5 results, best score 7.545 from QTM_CodeBase
- RAG all-collections query: 5 results from mixed collections
- RAG prefetch: 2,677 chars auto-injected context
- Cross-encoder reranking: scores range 1.0–7.5 for domain-specific queries (threshold 0.5 works)

---

## W. Phase M7 — Deployment & Cutover (2026-07-01)

**Goal:** Deploy Hermes gateway as the production agent, replacing the LangGraph Docker-based agent.

### Architecture change

Old: LangGraph agent in Docker container (`qnoe-agent:latest`) → `python -m agent.main`
New: Hermes gateway running natively as `qnoe-ai` user → `hermes gateway run`

Docker was dropped for the agent because:
- Hermes manages its own files (memory, skills, sessions) and needs write access
- No bind-mount complexity for all the model/data/config paths
- Simpler debugging and log access

### What was done

1. Created `start_hermes.sh` — sets env vars (HERMES_HOME, model paths, QDRANT_URL, etc.), sources `teams.env`, launches `hermes gateway run --replace -v`
2. Created `qnoe-hermes.service` — `User=qnoe-ai`, `Restart=on-failure`, `TimeoutStartSec=120`
3. Fixed bundled plugin issue: `Platform("teams_polling")` fails during config parsing if plugin is only in `$HERMES_HOME/plugins/` (user plugins discovered too late). Fix: copied plugin to `site-packages/plugins/platforms/teams_polling/`
4. Fixed file ownership: `chown -R qnoe-ai:qnoe-ai /opt/qnoe-agent/hermes/` (test runs left files owned by yzamir)
5. Fixed `teams.env` sourcing: changed `[ -f ... ]` to `[ -r ... ]` to avoid `set -e` crash when not readable
6. Set `GATEWAY_ALLOW_ALL_USERS=true` in start script (no per-user allowlist for Phase 1)
7. Set active profile: `hermes profile use qnoe-orchestrator`
8. Cutover: `systemctl stop qnoe-agent` + `systemctl disable qnoe-agent` → `systemctl enable qnoe-hermes` + `systemctl start qnoe-hermes`

### Deployment files

| File | Path on DGX |
|---|---|
| Start script | `/opt/qnoe-agent/scripts/start_hermes.sh` |
| Systemd unit | `/etc/systemd/system/qnoe-hermes.service` |
| Gateway wrapper (unused) | `/opt/qnoe-agent/hermes/scripts/gateway_wrapper.py` |
| Bundled plugin copy | `hermes-venv/.../plugins/platforms/teams_polling/` |

### Startup log

```
INFO hermes_plugins.teams_polling: Teams: authenticated as QNOE Agent (id=aa2b5ee6-...)
INFO hermes_plugins.teams_polling: Teams polling adapter connected
INFO gateway.run: ✓ teams_polling connected
INFO gateway.run: Gateway running with 1 platform(s)
```

### Rollback

```bash
sudo systemctl stop qnoe-hermes
sudo systemctl disable qnoe-hermes
sudo systemctl enable qnoe-agent
sudo systemctl start qnoe-agent
```

---

## X. Phase M7.6 — Full Feature Smoke Test (2026-07-02)

**Goal:** Verify all features work end-to-end after Hermes deployment.

### Results

| # | Test | Result | Notes |
|---|---|---|---|
| 1 | vLLM inference | PASS | Direct curl → correct response (vllm-0.22.1) |
| 2 | vLLM tool-calling | PASS | `read_file` tool call generated with correct JSON args |
| 3 | Qdrant RAG collections | PASS | 8 collections accessible, qtm has indexed code chunks |
| 4 | QCoDeS registry | PASS | 75,994 runs queryable (sample_name, exp_name, timestamps) |
| 5 | File read (CIFS) | PASS | `/ICFO/groups/NOE/Group_Manual.txt` readable |
| 6 | Directory listing | PASS | `/ICFO/groups/NOE/Lab_Instruments/` lists correctly |
| 7 | File search (repos) | PASS | `find` in `/opt/qnoe-agent/repos/` returns QTM python files |
| 8 | MEMORY.md persistence | PASS | Orchestrator MEMORY.md has group info, infrastructure facts |
| 9 | SOUL.md persona | PASS | Orchestrator + QTM profiles have correct personas + routing rules |
| 10 | Watcher daemon | PASS | `qnoe-watcher.service` active, scanning folders |
| 11 | Nightly cron | PASS | 02:00 cron configured with all env vars |
| 12 | Gateway → vLLM | PASS | `provider: custom` + `api_key: no-key-required` resolved correctly. `api_calls=1`, 19,761 in / 222 out tokens, 53s latency. 1,102-char response. (Initially failed with "Primary provider auth failed" — fixed by other agent changing provider config.) |
| 13 | Memory save + recall | PASS | Sent "Remember my favorite sample is BLG-device-7" → recalled correctly in follow-up |
| 14 | Context compression | PASS | Long detailed prompt processed, conversation continued working after |
| 15 | Skill creation | PASS | Agent created a temperature conversion skill when asked |

### Issues found and resolved during testing

1. **Provider auth failure (test 12):** Gateway could not resolve `provider: vllm-local` — Hermes auth resolver doesn't recognize custom provider names. Fixed by changing to `provider: custom` with explicit `api_key: no-key-required` and `base_url`.
2. **Profile dir permissions:** `/opt/qnoe-agent/hermes/profiles/qnoe-orchestrator/` is `0700/qnoe-ai` — `yzamir` user cannot run Hermes CLI directly. Tests 13–15 done via Teams instead.

**M7.6 smoke test: COMPLETE (15/15 PASS)**

---

## 2026-07-09 — Context-pressure package, steps 1-3

Executed [[CONTEXT_EXECUTION_PLAN]] (from [[CONTEXT_PRESSURE_REPORT]] §6). Branch `feature/context-pressure` off master.

**Step 1 — vLLM 64K + fp8 KV + max-num-seqs 4 (DEPLOYED).**
- `scripts/start_vllm.sh`: `--max-model-len 32768→65536`, added `--kv-cache-dtype fp8 --max-num-seqs 4`; also redirect stdout→`logs/vllm.log` (service otherwise only logs to journald, which is not readable by `yzamir`).
- Benchmarked fp8 vs fp16 (3× decode each, temp 0): **fp8 6.11 tok/s vs fp16 5.96 tok/s** (fp8 = 102% of fp16, ≥90% gate PASS); tool-call probe returns valid structured `tool_calls` in both; physics quality smoke coherent (no garbling). **fp8 shipped.**
- KV pool: fp8 **Available 67.4GB → 471,360 KV tokens → 7.19× concurrency @ 64K**; fp16 was 232,064 tokens / 3.54×. Both meet ≥3 users; fp8 doubles headroom.
- Hermes `context_length: 32768→65536` in all 3 profiles (orchestrator, qtm, photocurrent). Compaction now ~48K (threshold 0.75). `enable_prefix_caching=True` confirmed (vLLM V1).
- Agent restarted, healthy. **Teams round-trip NOT tested (cannot send Teams msgs) — human verification needed.**

**Step 2 — tool-schema slimming (DEPLOYED).**
- `toolsets: [hermes-cli, qnoe-lab]` → `[file, terminal, clarify, qnoe-lab]` (all 3 profiles). Verified core tools never defer (`_HERMES_CORE_TOOLS`); slimming via composition.
- Core tool schemas **6,054 → 3,550 tok (−2,504)**, measured with real tokenizer via vLLM `/tokenize`. Resident tools 12→7: `read_file, write_file, patch, search_files, terminal, process, clarify`. Dropped: skills, `memory`, `execute_code`. Floor ~11,725 → ~9,200.
- `hermes prompt-size` (ideal for floor) not runnable: profiles are 0700/qnoe-ai and `sudo -u qnoe-ai` is not in NOPASSWD; floor derived from measured tool delta.

**Step 3 — Provence reranker (EVAL ONLY; gate FAILED; NOT deployed).**
- Downloaded `naver/provence-reranker-debertav3-v1` @ `ef49e233` to `/home/yzamir/provence_dl` (1.74GB); installed `nltk` + `punkt_tab` in agent venv. Loaded OK under transformers 5.10.2.
- Offline eval, 20 QNOE queries (vLLM stopped to free RAM, restarted after): **token reduction 72%** (1631→454 top-3 tok, PASS), **answer survival 20/20** (PASS), **cpu latency 32.5× cross-encoder** (~22s/query vs 0.67s, **FAIL** ≤2×). Provence 0.4B DeBERTa on the Spark CPU is too slow; also exceeds the RAG prefetch 10s join timeout. Per plan §3.2 AND-gate → STOP, no deploy. `qnoe_rag` unchanged. Full report: `logs/provence_eval.md`.

**Not done (out of scope / gated):** Mem0 deploy, nightly cron re-enable, nightly SP task, steps 4-6. Leftovers on DGX: `/home/yzamir/provence_dl` (1.74GB, safe to delete), unused `nltk` in agent venv.
