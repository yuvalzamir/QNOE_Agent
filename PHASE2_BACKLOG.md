# QNOE Lab Agent — Phase 2 Backlog
*Last updated: 2026-05-29*

> Claude Code memory: [[HOME]] · Decisions: [[memory/decisions]] · Ingestion context: [[memory/ingestion]]

This document tracks features and improvements planned after the MVP (Phase 1 — Orchestrator + QTM + Photocurrent, T0/T1 read-only). Items are listed in rough priority order. Detailed design for each is in the linked documents.

---

## B1 — BM25 hybrid search (L2)
*From milestone plan Phase 5 · Design in `INFERENCE_MEMORY.md §L2`*

**What:** Add sparse BM25 vector support to the existing Qdrant collections alongside the dense vectors. Enables exact-term matching for device IDs (`SLG07-C2`), function names (`load_by_id`), and paper titles — queries where semantic similarity alone underperforms.

**Why now:** Requires no new infrastructure. Qdrant supports sparse vectors natively; it's an in-place upgrade to the existing L1 RAG layer.

**Tasks:**
- [ ] Enable BM25 sparse vectors in all 14 Qdrant RAG collections
- [ ] Update ingestion pipeline to generate sparse vectors alongside dense vectors at index time
- [ ] Update retrieval function to combine dense + sparse scores (reciprocal rank fusion or weighted sum)
- [ ] Evaluate exact-term recall improvement against the 20-question RAG evaluation set from L1 baseline

**Acceptance criteria:** Exact-term queries (device IDs, function names, paper titles) show measurable recall improvement vs L1 dense-only baseline.

**Depends on:** Phase 1 (L1 Qdrant RAG operational)

---

## B2 — Overnight measurement analysis
*No existing design doc — design needed*

**What:** A nightly scheduled job that identifies QCoDeS databases with new data from the day, verifies no measurement is currently running, runs analysis on the new data, and posts a summary — either as a file next to the data or to the relevant Teams sub-team channel (configurable).

**Why:** Researchers currently have to analyse each day's data manually the next morning. The agent can do a first-pass analysis overnight so results are waiting when they arrive.

---

### Sub-problem 1 — Identifying DBs with new data

The nightly re-indexing run (02:00 cron) already computes SHA-256 hashes of all `.db` files and stores them in the SQLite `index_manifest` table. The analysis job can query this table for files whose hash changed since the previous night's run — no extra scanning needed.

**Tasks:**
- [ ] Add a query to the nightly job: `SELECT path FROM index_manifest WHERE last_modified > yesterday AND content_type = 'qcodes_db'`
- [ ] Confirm the `index_manifest` schema stores per-file timestamps suitable for this query (may need a `last_seen_changed` column added)

---

### Sub-problem 2 — Confirming no measurement is running

QCoDeS databases write continuously for extended periods with no clean "done" signal (this is why a real-time DB trigger was rejected in `AGENT_FRAMEWORK.md §7.3`). The overnight job avoids this by running at 02:00 and applying a **stationary check** before touching any file:

**Proposed approach:** check that the file size has not changed over a configurable polling window (e.g. 3 checks x 5 minutes apart = 15 minutes). If size is still changing, skip that DB for tonight and log it.

This is a best-effort safety measure, not a hard guarantee — if a measurement runs unusually late, the file will simply be skipped and analysed the following night.

**Tasks:**
- [ ] Implement `is_db_stationary(path, checks=3, interval_seconds=300) -> bool`
- [ ] Add configurable threshold to `triggers.yaml`:
  ```yaml
  overnight_analysis:
    stationary_checks: 3
    stationary_interval_seconds: 300
    run_after_hour: 2   # don't start before 02:00
  ```
- [ ] Log skipped DBs to SQLite and notify sub-team channel: "Skipped `<file>` — still being written at analysis time. Will retry tomorrow night."

> **Open question:** Is there a lock file, process signal, or instrument software flag that could give a more reliable "measurement running" indicator? If the measurement software writes a `.lock` file or sets a flag in the DB itself, that would be more reliable than the size-polling approach. **Ask the QTM and Photocurrent teams** before implementing.

---

### Sub-problem 3 — Analysis skill architecture

The analysis skill is split into a **main skill** and **per-setup sub-skills**. The main skill can be written once and works for all sub-teams. The sub-skills are written per experimental setup and require researcher input.

#### 3a — Main skill (universal)

The main skill is a context assembly pipeline. It does not interpret physics — it gathers everything the LLM needs to reason well about the data. These steps are identical regardless of experiment type:

```
1. Read QCoDeS metadata
     - experiment name, station config, parameter names and units,
       sweep ranges, timestamps, linked device ID
     - calls B3: get_run_metadata() + load_dataset()

2. Read repo documentation
     - fetch README and any docs/ files from the measurement repo
       (identified from the DB path or QCoDeS station config)
     - this tells the LLM what this setup measures and what the
       standard workflow is

3. Retrieve similar past measurements from the group corpus
     - NOTE: this is NOT the same as conversational RAG over prose/code
     - requires querying QCoDeS metadata stored in the index_manifest:
       same device ID, same measured parameters, similar temperature/field
     - returns: past run summaries and any linked analysis notebooks
     - purpose: give the LLM a comparison baseline ("last week this
       device showed X; today it shows Y")

4. Load the setup sub-skill (see 3b)
     - inject YAML as structured context, not as executable code
     - use YAML to select which setup-specific scripts to run (step 5)

5. Run Python scripts to compute outputs
     - always run: basic_stats.py, detect_anomalies.py, plot_1d/2d as appropriate
     - if setup identified: run setup-specific scripts (e.g. didv.py, photocurrent_map.py)
     - if setup unknown: run generic scripts only
     - outputs: statistics dict, figure paths, computed quantities

6. Assemble full context → call Hermes → generate analysis output
     - context includes: metadata (step 1), repo docs (step 2), past
       measurement summaries (step 3), YAML expectations (step 4),
       script outputs — stats + figure descriptions (step 5)
     - LLM task: interpret results, flag anomalies against YAML expectations,
       compare to past measurements, write summary
```

```
/opt/qnoe-agent/skills/
  qcodes_analysis/
    v1/
      skill.py              # main skill: orchestrates steps 1-5
      scripts/
        generic/            # deterministic scripts, setup-agnostic
          load_db.py        # open QCoDeS DB, extract datasets and metadata
          basic_stats.py    # min/max/mean/std per parameter, NaN count
          plot_1d.py        # generic 1D sweep plot (any x vs any y)
          plot_2d.py        # generic 2D map (any x/y vs z, with colorbar)
          detect_anomalies.py  # generic: NaN runs, clipped values, noise floor
        setup_specific/     # deterministic scripts, per setup
          qtm_cryo/
            didv.py         # compute dI/dV from lock-in X/Y, plot spectrum
            conductance_map.py  # 2D dI/dV map with tip position
          grasp_photocurrent/
            photocurrent_map.py  # 2D photocurrent vs gate/position
            gate_sweep.py   # resistance vs gate voltage, extract CNP
          ...
      setups/               # YAML sub-skills: measurement expectations (see 3b)
        qtm_cryo.yaml
        grasp_photocurrent.yaml
        ...
```

**The role of each layer is distinct:**

| Layer | Who writes it | What it does | LLM involved? |
|---|---|---|---|
| `scripts/generic/` | Agent developer (once) | Load, plot, basic stats — works on any QCoDeS DB | No |
| `scripts/setup_specific/<setup>/` | Developer + researcher | Compute physically meaningful quantities for that setup | No |
| `setups/<setup>.yaml` | Researcher | Describe measurement types, expected ranges, anomaly flags | Read as context |
| `skill.py` | Agent developer | Orchestrate: run scripts, collect outputs, assemble context, call LLM | Calls LLM at the end |

The LLM receives the *outputs* of the scripts as context — figures, statistics, computed quantities — not raw data arrays. It never does arithmetic or signal processing itself.

**Tasks:**
- [ ] Design main skill interface spec (inputs: db path; outputs: summary text, figure paths, JSON sidecar)
- [ ] Use B3 (`load_dataset` + `get_run_metadata`) for DB access — do not re-implement
- [ ] Implement `scripts/generic/basic_stats.py` — min/max/mean/std/NaN count per parameter
- [ ] Implement `scripts/generic/plot_1d.py` and `plot_2d.py` — generic plotting for any parameter combination
- [ ] Implement `scripts/generic/detect_anomalies.py` — NaN runs, clipped/saturated values, noise floor exceeded
- [ ] Implement repo doc fetcher (step 2) — uses git to pull README + docs/ from the relevant repo
- [ ] Design and implement QCoDeS metadata retrieval (step 3) — distinct from prose RAG; queries `index_manifest` by device ID + parameter names
- [ ] Implement sub-skill loader (step 4) — loads YAML, selects which setup-specific scripts to invoke
- [ ] Implement script runner (step 5) — executes selected scripts, collects outputs into context
- [ ] Implement context assembly and Hermes call (step 6)
- [ ] Define output format: `.md` summary + `.json` sidecar + `.png` figures
- [ ] Register in skill registry

#### 3b — Setup sub-skills (per experimental setup, researcher-authored)

Each sub-skill is a structured document (YAML) that describes what a normal measurement on that setup looks like. The main skill loads it as context — no code, no execution. This keeps sub-skills easy for researchers to write and update without touching Python.

**What a sub-skill contains:**
```yaml
# Example: setups/qtm_cryo.yaml
setup_name: QTM Cryogenic
repos: [QTM_CodeBase, L208_Opticool]
measurement_types:
  - name: dI/dV spectroscopy
    parameters: [bias_voltage, current, lock_in_x]
    expected_range: {bias_voltage: [-100mV, 100mV]}
    standard_plots: [dIdV_vs_bias, conductance_map]
    anomaly_flags: [zero_current_plateau, noise_floor_exceeded]
  - name: topography scan
    parameters: [x, y, z, current]
    standard_plots: [topography_2d, height_histogram]
    anomaly_flags: [tip_crash_signature, drift_artefact]
notes: >
  Measurements run at 4K in the Opticool. Lock-in frequency is 177 Hz.
  A good dI/dV spectrum shows clear peaks at ±eV corresponding to the
  tip DOS. Flat featureless spectra usually indicate a bad tip.
```

The LLM reads this YAML as plain text context and uses it to:
- Identify which measurement type is in the DB
- Know what plots to generate
- Know what counts as anomalous

**Tasks:**
- [ ] Define the YAML schema for sub-skills (fields above are a draft)
- [ ] Write `qtm_cryo.yaml` — with QTM sub-team
- [ ] Implement `scripts/setup_specific/qtm_cryo/didv.py` — compute dI/dV from lock-in X/Y channels, plot spectrum — with QTM sub-team
- [ ] Implement `scripts/setup_specific/qtm_cryo/conductance_map.py` — 2D dI/dV map — with QTM sub-team
- [ ] Write `grasp_photocurrent.yaml` — with Photocurrent sub-team
- [ ] Implement `scripts/setup_specific/grasp_photocurrent/photocurrent_map.py` — with Photocurrent sub-team
- [ ] Implement `scripts/setup_specific/grasp_photocurrent/gate_sweep.py` — with Photocurrent sub-team
- [ ] Add remaining setups as each sub-team is onboarded (Phase 3+)

> **These require researcher time.** For each setup, schedule two sessions: (1) fill in the YAML together — what do you measure, what do you plot every morning, what does a failed run look like; (2) review the setup-specific scripts for correctness — the researcher must validate that `didv.py` is computing the right thing before it runs unsupervised overnight.

---

### Sub-problem 4 — Posting results

Two output destinations, configurable per sub-team:

| Destination | Mechanism | When to use |
|---|---|---|
| **File alongside data** | Write `<db_name>_agent_analysis_<date>.md` + `.json` + `.png` to the same directory on `/mnt/qnoe-data` | Always — regardless of Teams config |
| **Teams sub-team channel** | Post summary text + embedded figure to `#qtm` / `#photocurrent` | Configurable; on by default |

The file-alongside-data approach is always done — it creates a permanent record even if Teams is unavailable. The Teams post is a notification layer on top.

**Teams post format:**
```
Overnight analysis — <device_name> — <date>
<3-sentence summary>
[Full report: /mnt/qnoe-data/<path>/<db_name>_agent_analysis_<date>.md]
```

**Tasks:**
- [ ] Add output config to `triggers.yaml`:
  ```yaml
  overnight_analysis:
    post_to_teams: true
    write_file: true   # always recommended
  ```
- [ ] Implement file writer (markdown summary + JSON sidecar + figures)
- [ ] Implement Teams post formatter and sender
- [ ] Handle the case where the data directory is read-only for the agent account — fall back to a designated output subdirectory (e.g. `/mnt/qnoe-data/agent_outputs/`)

---

### Overall task ordering

1. `index_manifest` schema confirmed / extended
2. `is_db_stationary()` implemented and tested
3. Generic scripts implemented (`load_db`, `basic_stats`, `plot_1d/2d`, `detect_anomalies`)
4. YAML sub-skill schema defined; `qtm_cryo.yaml` written with QTM team
5. QTM setup-specific scripts written and **researcher-validated** (`didv.py`, `conductance_map.py`)
6. Main skill orchestrator implemented (steps 1–6); file writer implemented
7. End-to-end test on a real (old) QCoDeS DB from QTM — no live data yet
8. `grasp_photocurrent.yaml` + Photocurrent scripts written and validated; end-to-end test
9. Teams posting enabled
10. Nightly cron job wired up — first week: dry run only (outputs written to files, no Teams post)
11. Live production after dry-run review
12. Remaining setup YAMLs + scripts added as each sub-team is onboarded (Phase 3+)

**Acceptance criteria:**
- Agent correctly skips a DB that is still being written (size-polling test)
- Agent produces a valid `.md` + `.json` + `.png` for a known QTM dataset
- Teams post appears in the correct sub-team channel with correct attribution
- No corruption or partial reads of any active measurement DB

**Depends on:** Phase 1 (data server mount operational, sub-agent Teams posting working) · L4 skill registry · **B3** (QCoDeS data access skill) · B1 not required

---

## B3 — QCoDeS data access skill (generic)
*No existing design doc — design needed*

**Predecessor:** This skill is the Phase 2 evolution of the QCoDeS Registry + Summary Cards system (see `AGENT_FRAMEWORK.md §QCoDeS measurement data`). Phase 1 provides passive discovery via semantic search over summary cards in the `qcodes-runs` Qdrant collection and a `qcodes_registry` SQLite table. B3 adds active, on-demand access — the agent can open a database live, extract specific run data, and answer precise questions that summary cards alone cannot handle (e.g. "what was the noise floor in run 42?" or "plot the last gate sweep").

**What:** A standalone, callable skill that gives any sub-agent read access to QCoDeS databases on the data server. This is the foundational data layer — both for interactive queries ("what did we measure yesterday on SLG07?") and as a dependency for the overnight analysis (B2).

**Why separate from B2:** This capability is useful far beyond overnight analysis. A researcher asking "what was the noise floor in last week's transport run?" should be answerable on demand without triggering a full analysis pipeline. The skill is general; B2 is one consumer of it.

**Relationship to Phase 1 QCoDeS registry:** B3's `list_recent_runs()` queries the `qcodes_registry` table populated by the nightly scanner — it does not scan the filesystem. The registry is both the Phase 1 deliverable and the discovery index for B3.

---

### What the skill exposes

The skill is a set of callable functions injected into the sub-agent's tool list:

```python
list_recent_runs(
    device_id: str | None,       # e.g. "SLG07", or None for all
    since: str | None,           # ISO date, e.g. "2026-05-20"
    sub_team: str | None,        # filter by data server subtree
) -> list[RunSummary]
# Returns: run ID, experiment name, date, parameter names, row count, file path

load_dataset(
    db_path: str,
    run_id: int,
    parameters: list[str] | None,  # None = all parameters
    max_rows: int = 10_000,        # safety cap — never loads unbounded arrays
) -> Dataset
# Returns: parameter arrays + units + metadata dict

get_run_metadata(
    db_path: str,
    run_id: int,
) -> Metadata
# Returns: station config, snapshot, experiment name, timestamps — no data arrays
```

`get_run_metadata` is always safe and fast — no data loaded. `load_dataset` has a hard row cap so the agent cannot accidentally pull a multi-GB array into memory. The LLM calls these functions; it never touches the DB file directly.

### What it does NOT do

- No writing to any DB — read-only by design
- No data processing or plotting — that is B2's job
- No access outside `/mnt/qnoe-data` — path is validated against the mount root before any open

### Tasks

- [ ] Design `RunSummary`, `Dataset`, `Metadata` TypedDicts (the skill's output schema)
- [ ] Implement `list_recent_runs()` — queries `index_manifest` SQLite table; no direct filesystem scan
- [ ] Implement `get_run_metadata()` — opens DB read-only, extracts QCoDeS snapshot and station config
- [ ] Implement `load_dataset()` — opens DB read-only, extracts parameter arrays up to `max_rows`; raises if path is outside mount root
- [ ] Write skill interface spec (function signatures + docstrings) for injection into system prompt
- [ ] Register in skill registry; inject into all sub-agent system prompts
- [ ] Test: confirm read-only (no writes possible), path validation blocks traversal attempts, row cap enforced

### Acceptance criteria

- Any sub-agent can answer "what runs exist on SLG07 from last week?" without accessing the filesystem directly
- `load_dataset` on a 1M-row DB returns only the first 10,000 rows and says so
- Attempting to open a path outside `/mnt/qnoe-data` raises a clear error

**Depends on:** Phase 1 (data server mount operational) · L4 skill registry

---

## B5 — OCR pipeline for scanned PDFs — RESOLVED (2026-06-19)

**What:** Many PDFs in the lab corpus were logged to `/tmp/empty_pdfs.log` because pypdf extracted <200 chars. These were assumed to be scanned PDFs needing OCR.

**What was done:**
- GPU OCR run completed (2026-06-18): 10,027 files processed with Docling + `DOCLING_DEVICE=cuda`
- Second run (2026-06-19): 10,872 files processed
- **Result: 1 chunk total across both runs**

**Root cause investigation (2026-06-19):** Inspected 10 random samples from the 10,873 files. They are **not scanned documents** — they are **single-page matplotlib/instrument-generated plots** containing only axis labels and tick values as text (47–121 chars each). pypdf correctly extracts the text; there is simply very little text to extract.

**Breakdown of the 10,873 "empty" PDFs:**

| Category | Count | % | What they are |
|---|---|---|---|
| Noise measurements | 4,691 | 43% | Noise spectroscopy plots (SV vs f, mobility, mfp) |
| FTIR | 2,551 | 24% | Extinction/transmission spectra |
| Other (figures, Keynote temps) | 2,363 | 22% | Paper figures, presentation image exports, Raman maps |
| Transport | 914 | 8% | Rxx, Rxy vs gate voltage plots |
| matplotlib test baselines | 354 | 3% | Anaconda test suite artifacts — junk |

**Decision: do not index these.** Axis labels alone ("Rxx (kΩ)", "n (cm⁻²)") are not useful for RAG — they'd match too many queries without providing answers. The filenames and folder paths contain more information (device ID, measurement type, parameters) than the extracted text. The 354 matplotlib test baseline files are pure junk.

**Future option:** See B6 — VLM-based figure description could make these plots searchable by generating text summaries of the visual content.

---

## B6 — VLM figure description for plot PDFs

**What:** Use a vision-language model to generate text descriptions of the ~10k single-page plot PDFs identified in B5. These are matplotlib/instrument-generated figures containing measurement data as images. A VLM could produce summaries like "Noise spectrum showing 1/f behavior, SV vs frequency for a graphene device at gate voltage 30V" — making the plots searchable by content rather than just by filename.

**Why deferred:** Requires a VLM (either local or remote via B4). Not critical for MVP — the data is already discoverable via filenames, folder paths, and the associated QCoDeS databases.

**Proposed approach:**
1. Render each single-page PDF to a PNG (PyMuPDF `page.get_pixmap()`)
2. Send to a VLM with a structured prompt: "Describe this scientific plot. Include: plot type, axes, key features, approximate parameter ranges."
3. Index the generated description as a text chunk in the appropriate Qdrant collection
4. Store the description alongside the file in the manifest

**Tools considered:**
- **PyMuPDF** — fast PDF→PNG rendering, good for vector PDFs
- **plotdigitizer** / **svgdigitizer** — extract numerical data points from plots (overkill for RAG indexing)
- **Local VLM** — would need a multimodal model; Hermes 3 is text-only
- **Remote VLM via B4** — Claude/GPT-4V could describe figures, but 10k calls is expensive and requires B4 infrastructure

**Tasks:**
- [ ] Evaluate local multimodal models that fit alongside vLLM (e.g. LLaVA variants, <8GB)
- [ ] Alternatively, batch through B4 remote path with anonymised figures
- [ ] Implement `describe_figures.py` — PDF→PNG→VLM→text pipeline
- [ ] Index generated descriptions in Qdrant
- [ ] Evaluate description quality on 20 sample plots

**Depends on:** B4 (frontier model access) or a local multimodal model · Phase 1 complete

---

## B7 — Migrate back to NVIDIA OpenShell sandbox

> **🔴 PROMOTED TO HIGH PRIORITY (2026-07-14):** red-team R4 proved the agent can write to lab files (read-only is SOUL-only, unenforced). Re-enabling this sandbox is the fix. See [[TODO]] + `redteam/BACKLOG.md` Round 2b. Gaps to close: add `/opt/qnoe-agent/repos` to the policy read_only list; add `localhost:8000` to the network policy.
>
> **✅ READ-ONLY ENFORCEMENT RESOLVED 2026-07-14 — via systemd, not OpenShell.** `qnoe-hermes.service` drop-in `50-b7-readonly.conf` (mount-namespace `ReadOnlyPaths`/`InaccessiblePaths`) now physically blocks all writes to repos / /ICFO / config and hides `secrets/`; verified by the `qnoe-b7-test.service` probe (19/19 PASS). Both policy gaps above were also closed in `sandbox-policy.yaml`. **What remains of B7 is only the OpenShell migration itself** — its added value over the systemd fix is network L7 inspection + credential scoping (needed for Phase 2 T2–T4), not read-only enforcement. Demoted back to normal priority.

**What:** Phase 1 bypasses OpenShell and uses plain `docker run` because OpenShell v0.0.59 silently ignores user volume mounts (`--driver-config-json` Docker mounts are marked "Experimental" and not implemented for the Docker driver). Once NVIDIA ships a version that supports volume passthrough, migrate back to OpenShell for its network L7 inspection, credential scoping, and JWT-based sandbox auth.

**Why:** OpenShell provides policy enforcement layers (network filtering, credential isolation) that plain Docker doesn't. These matter for Phase 2 (T2–T4 write access) where the agent can modify shared resources.

**Trigger:** Monitor OpenShell releases for Docker driver mount support. Check release notes at each NVIDIA DGX software update. *(Re-checked 2026-07-14: still 0.0.59, mounts still experimental/K8s-only — blocked.)*

> **🟢 UNBLOCKED (2026-07-14, web check):** current OpenShell releases (latest **v0.0.82**, 2026-07-13) support Docker-driver **volume/bind/tmpfs mounts** via `--driver-config-json '{"docker":{"mounts":[...]}}'`, gated by `[openshell.drivers.docker] enable_bind_mounts = true` in `gateway.toml` (bind mounts default `read_only: true`; SELinux labels since v0.0.76). Our existing `launch_sandbox.sh` JSON already matches the now-implemented schema. Path: upgrade the DGX deb 0.0.59→latest (arm64 on GitHub releases; nothing in production uses openshell today, so upgrade is low-risk), set `enable_bind_mounts`, expect a compat pass over policy YAML/CLI (23 releases of drift; e.g. v0.0.74 rejects whitespace in mount fields). NVIDIA's own docs warn bind mounts "can negate OpenShell controls" — we deliberately accept that: our mounts ARE the policy (ro flags per `config/sandbox-policy.yaml`), and we still gain landlock, the sandbox user, L7 network policies, and the audit trail. Docs: https://docs.nvidia.com/openshell/reference/sandbox-compute-drivers · https://github.com/NVIDIA/OpenShell/releases
>
> **✅ B7 CLOSED — EXECUTED 2026-07-14 (same day):** OpenShell v0.0.82 migration done in one session; `qnoe-hermes-sandbox.service` is production (enabled at boot), systemd unit retained as rollback. Full record: [[memory/decisions#D18]], [[memory/infrastructure]] §B7-OS, `memory/mistakes.md` M50/M51. Remaining hardening (new backlog items, NOT blockers): dedicated sandbox uid (identity isolation), OpenShell inference proxy (`local-vllm` → `https://inference.local/v1`) for LLM-path audit/credential scoping, retire the systemd drop-in after weeks of stability.

**Migration plan + estimates (2026-07-14, user wants off systemd):**
1. **De-lock-in (DONE 2026-07-14):** `config/sandbox-policy.yaml` is the single source of truth for the confinement contract; `scripts/b7_probe.sh` is the mechanism-agnostic acceptance test (runs inside any confinement). The only systemd-specific artifacts are the drop-in + probe launcher unit — both disposable. Gateway code has zero systemd references; `start_hermes.sh` credential chain already works under systemd/docker/bare.
2. **Interim, available now — plain Docker (~1–2 days + verification day):** rebuild image for the Hermes runtime (arm64, match hermes-venv python), `docker run` with ro binds per the policy, rw volumes memory/logs/hermes, `--env-file` for teams.env, tmpfs /tmp, run as 1001. Buys immutable code view + identity isolation + an egress hook. Domain-level egress (graph.microsoft.com) needs an L7 proxy: **+1 day**, else coarse L3/L4 only.
3. **Target — OpenShell (~2–3 days once NVIDIA ships Docker-driver mounts):** step 2's image + mount map carry over; add landlock policy wiring, OpenShell network policies (the L7 part replaces the proxy), JWT/gateway auth, full re-verification.

**Tasks:**
- [ ] Monitor OpenShell releases for volume mount support in the Docker driver
- [ ] Re-test with the new version: create sandbox with user volumes, verify via `docker inspect`
- [ ] Restore `sandbox-policy.yaml` enforcement (network L7, credential scoping)
- [ ] Update `qnoe-agent.service` to depend on `openshell-gateway.service` again
- [ ] Remove plain `docker run` start script

**Depends on:** NVIDIA shipping OpenShell with working Docker volume mounts

---

## B4 — Frontier model access for high-complexity tasks
*No existing design doc — design needed*

**What:** Allow the agent to route selected requests to an external frontier model (e.g. Claude, GPT-4) when local Hermes 3 70B is insufficient. The switch must be seamless, tightly guarded, and transparent to the user. This partially relaxes the "no data leaves the lab network" principle — so guardrails are non-negotiable.

**Why:** Some tasks genuinely exceed what a 70B local model can do reliably: nuanced multi-step reasoning over complex experimental results, sophisticated cross-domain synthesis, or cases where the user explicitly wants the best available reasoning. Rather than pretending the local model is always sufficient, expose frontier access in a controlled, audited way.

---

### Sub-problem 1 — When to use a remote model

Three trigger conditions, in order of priority:

| Trigger | How detected | Notes |
|---|---|---|
| **Explicit user request** | User types `/expert` or "use Claude for this" | Highest priority — always honoured if within scope |
| **Task classification** | Orchestrator classifies incoming request as high-complexity before doing anything else | See classification rules below |
| **Local model signals low confidence** | Hermes returns a hedged or incomplete response on first attempt | Optional fallback path; adds latency |

**Classification rules (run locally on Hermes — never send data to decide whether to send data):**

```
High-complexity → remote eligible:
  - Multi-step experimental data interpretation ("explain why these two runs differ")
  - Cross-domain synthesis across sub-teams
  - Novel anomaly explanation (nothing similar in group corpus)
  - User explicitly asks for deep reasoning or "best answer"

Local-only → always stays local:
  - Routine RAG Q&A ("what function loads a QCoDeS dataset?")
  - Repo monitoring and proactive triggers
  - Any T2–T4 action decision (write/destructive actions never touch remote models)
  - Any request that includes raw data arrays or file paths with device identifiers
```

**`model_tier` field in `AgentState`:**

Add one field to the existing TypedDict (see `AGENT_FRAMEWORK.md §4.2`):

```python
class AgentState(TypedDict):
    ...
    model_tier: Literal["local", "remote"]   # default: "local"
    remote_reason: str | None                # why remote was selected; logged and shown to user
```

LangGraph sets this at the routing node. All downstream nodes read it and pass it to the `ModelRouter`.

---

### Sub-problem 2 — The switch mechanism (`ModelRouter`)

`ModelRouter` is custom code — not a LangGraph built-in (LangGraph routes between graph nodes, not between LLM endpoints) and not a Hermes feature (Hermes is just the model). It is a small class, instantiated once at startup, that presents one interface to the rest of the agent:

```python
class ModelRouter:
    def __init__(self):
        self.local = openai.AsyncOpenAI(base_url="http://localhost:8000/v1", api_key="not-needed")
        # remote client only initialised if frontier_models.enabled: true in config

    async def complete(self, messages: list, state: AgentState, **kwargs) -> str:
        if state["model_tier"] == "remote" and self._remote_permitted(state):
            return await self._remote_complete(messages, state, **kwargs)
        return await self._local_complete(messages, **kwargs)
```

Switching on the fly is just setting `state["model_tier"] = "remote"` in LangGraph — no restarts, no separate agent instances. For the remote path, `_remote_complete` calls the open shell wrapper (Sub-problem 3) rather than opening an external socket directly.

> **Note on OpenRouter:** OpenRouter (openrouter.ai) is a third-party cloud service that aggregates many model APIs behind one OpenAI-compatible endpoint. It is not a Hermes feature. It would simplify the client code (one endpoint instead of two) but data still transits their servers — same concern as going directly to Anthropic. Not recommended here.

**Config in `triggers.yaml`:**
```yaml
frontier_models:
  enabled: false          # off by default; maintainer enables explicitly
  provider: anthropic     # anthropic | openai
  model: claude-opus-4-6
  max_requests_per_day: 50   # hard cap; enforced by ModelRouter
  allowed_triggers: [explicit, task_classification]
```

---

### Sub-problem 3 — Open shell guardrails (both modes)

The open shell enforces both modes using the same mechanism it always uses — OS account permissions — applied twice, with two accounts that have **complementary, mutually exclusive capabilities**.

**Two OS accounts:**

| | `qnoe-ai` (local mode) | `qnoe-ai-remote` (remote call account) |
|---|---|---|
| `/mnt/qnoe-data` access | Full read + write | No mount — account cannot see this path |
| Lab git credentials | Yes | No |
| Lab network | Yes | Yes |
| External internet | No | Yes — whitelisted hosts only (`api.anthropic.com` etc.) |
| Frontier API key | No | Yes — loaded from `/opt/qnoe-agent/secrets/frontier_api_key` |

The agent runs as `qnoe-ai` at all times. `remote_llm_call.py` runs as `qnoe-ai-remote` via a specific `sudo` rule (no password, scoped to that script only). When the wrapper is invoked, it is **literally incapable** of reading the data server or lab secrets — not because application code checked something, but because the OS account has no access. This makes data minimization an OS-enforced guarantee, not an application-level one.

```
# /etc/sudoers entry (the only sudo rule the agent account gets)
qnoe-ai ALL=(qnoe-ai-remote) NOPASSWD: /opt/qnoe-agent/bin/remote_llm_call.py
```

```
Allowed commands list (DGX_SETUP.md §11) — add:
  remote_llm_call.py   # invoked via sudo; runs as qnoe-ai-remote
```

`remote_llm_call.py` still enforces application-level checks as defence-in-depth:
- Logs every call to the audit trail *before* making it
- Enforces daily request cap (reads/writes a counter in the episodic SQLite DB — `qnoe-ai-remote` is granted read/write access to this DB; no other `qnoe-ai` resources are shared)
- Applies data minimization regex blocklist (see Sub-problem 4) — OS permissions are the primary control; the blocklist is a secondary check
- Temp directory permissions and vision capability config flag: decided at implementation time

---

### Sub-problem 4 — What gets sent: prompt construction as the primary control

The OS account switch (Sub-problem 3) prevents `qnoe-ai-remote` from loading more data. But the prompt handed to it was assembled by `qnoe-ai` beforehand — and that context may already contain device names, file paths, run IDs from RAG chunks or episodic memory. Applying a regex blocklist to a full LLM context window is fragile: device names can be paraphrased, paths appear in unexpected formats, and the blocklist is always playing catch-up.

**The real control: never build a full context window for the remote call.**

The remote prompt is not the same assembled context used for local calls. Before the sudo switch, the local Hermes model runs a sanitisation step: it reads the full local context and produces a **clean problem statement** — describing the physics question without identifying metadata. Only this sanitised statement + the user's literal question are passed to `remote_llm_call.py`.

```
Local Hermes (full context) → sanitisation prompt → clean problem statement
                                                              ↓
                                                   remote_llm_call.py (sudo)
                                                              ↓
                                                    External API call
```

**Sanitisation prompt (run locally before the sudo call):**
```
You are preparing a question for an external AI model. The external model must not
receive any identifying information about the lab, devices, or researchers.

Rewrite the following context as a physics/methods question only:
- Replace all device identifiers (e.g. SLG07, BLG-QED) with generic terms
  (e.g. "a graphene device", "a BLG sample")
- Remove all file paths, run IDs, timestamps, and researcher names
- Keep the physics: parameter ranges, observed behaviour, anomalies
- If the question cannot be asked without identifying information, return:
  CANNOT_SANITISE: <reason>

Context: {full_local_context}
User question: {user_question}
```

If Hermes returns `CANNOT_SANITISE`, the remote call is aborted and the request is handled locally.

**What each context slot contributes to the remote prompt:**

| Local context slot | Goes to remote prompt? | Why |
|---|---|---|
| User's question text | Yes — verbatim | Needed for the question |
| Hermes-generated sanitised summary | Yes | The only way local context reaches remote |
| Raw RAG chunks | Never | May contain device IDs, file paths |
| Episodic context (task history) | Never | Contains run IDs, repo names, file paths |
| Mem0 user facts | Never | Contains project affiliations, device names |
| System prompt | Minimal version only | Full system prompt contains lab-specific context |

**The regex blocklist in `remote_llm_call.py` is defence-in-depth**, not the primary control. Its job is to catch sanitisation failures — cases where Hermes produced a summary that still contains an identifier. If triggered, the call is refused, logged, and falls back to local. The patterns are stored in `config/data_minimization.yaml` and defined at implementation time.

**What the remote model receives — and nothing else:**
```
[System]: You are a physics and data analysis expert. Answer the following question.
[User]: {sanitised_problem_statement}
          {user_question_verbatim}
          [optional: anonymised figures or tabular data — see below]
```

**What this means in practice:** the remote model is used as a reasoning engine on an anonymised problem, not as a knowledgeable participant in the lab's work. It never builds up knowledge of QNOE devices or projects across calls — each call is a self-contained anonymised question.

---

### Sub-problem 4b — Sending actual data to the remote model

The "no data leaves the network" principle targets **identity** (device names, run IDs, researcher names, file paths), not **physics** (numerical arrays, measurement patterns, physical units). The two can be separated, which means actual measurement data can be sent to a remote model in an anonymised form.

**Two mechanisms, in order of preference:**

#### Mechanism 1 — Anonymised figures (preferred)

The B2 analysis skill already generates `.png` plots. If those plots are rendered without identifying information in titles, labels, or captions — just physical axes ("Gate voltage (V)", "Conductance (e²/h)") — they contain the measurement pattern the remote model needs to see, with no identifying information. Vision-capable frontier models (Claude Opus, GPT-4V) can interpret these directly.

What is allowed in a figure sent to a remote model:
- Physical axis labels and units
- Colorbars with physical units
- Generic titles ("dI/dV spectrum", "Gate sweep")

What must be absent:
- Device names (SLG07, BLG-QED)
- Run IDs, timestamps, temperatures unless physics-relevant and generic
- Researcher names, file paths

The B2 setup-specific scripts (Sub-problem 3b) must generate anonymised figures by default. This is enforced by a figure generation wrapper that strips the title and validates labels before saving.

#### Mechanism 2 — Anonymised tabular data

For cases where the model needs to work with the raw numbers, not just a visual:

Strip all identifying metadata from the dataset. Preserve physics vocabulary — "Gate voltage (V)" and "Conductance (e²/h)" are not identifying, they are standard physics terms. What is identifying is everything outside the column content itself.

```python
# What gets stripped:
{
  "device": "SLG07-C2",       # → removed
  "run_id": 4821,              # → removed
  "date": "2026-05-12",       # → removed
  "temperature": "4K",         # → kept if physics-relevant, as "cryogenic (4K)"
  "gate_voltage_V": [...],    # → kept, renamed to "Gate voltage (V)"
  "conductance_e2h": [...],   # → kept, renamed to "Conductance (e²/h)"
}

# What is sent:
{
  "Gate voltage (V)": [...],
  "Conductance (e²/h)": [...],
  "conditions": "cryogenic measurement"
}
```

The stripping is done by `qnoe-ai` (local account, full context) as part of sanitisation, before the payload is handed to the wrapper.

#### Transport: how data crosses the account boundary

`qnoe-ai-remote` has no `/mnt/qnoe-data` mount, so it cannot read raw data files. `qnoe-ai` prepares the anonymised payload and writes it to a designated temp directory:

```
/opt/qnoe-agent/tmp/remote_payloads/<uuid>/
  payload.json        # anonymised tabular data (if used)
  figure_1.png        # anonymised figure (if used)
  prompt.txt          # sanitised text prompt
```

`remote_llm_call.py` only reads from this one directory — it refuses to read arbitrary paths. After the call completes (success or failure), the wrapper deletes the temp directory.

```
qnoe-ai (full context)
  → sanitise data / generate anonymised figures
  → write to /opt/qnoe-agent/tmp/remote_payloads/<uuid>/
  → sudo remote_llm_call.py <uuid>
      (qnoe-ai-remote)
      → read payload from /opt/qnoe-agent/tmp/remote_payloads/<uuid>/
      → blocklist check on prompt.txt
      → send to external API
      → delete temp directory
      → return response
```

**This preserves the two-account security model:** `qnoe-ai` does all data access and sanitisation; `qnoe-ai-remote` only transmits what it's given and cannot reach back into the data server for more.

**Tasks (additions to B4 task list):**
- [ ] Implement figure generation wrapper: validates no device names in title/labels before saving `.png`
- [ ] Implement tabular data anonymiser: strips identifying fields, preserves physical column names
- [ ] Implement temp payload directory: write/read/delete lifecycle in `remote_llm_call.py`
- [ ] Restrict `remote_llm_call.py` to only read from `/opt/qnoe-agent/tmp/remote_payloads/` — refuses arbitrary paths
- [ ] Test: figure with device name in title is rejected by the figure wrapper
- [ ] Test: tabular payload written by `qnoe-ai` is readable by `qnoe-ai-remote` from temp dir only
- [ ] Test: temp directory is deleted after call regardless of success or failure

---

### Sub-problem 5 — User transparency

The user must always know when a remote model is handling their request. This is non-negotiable — both for trust and for data consent.

**Before the remote call:**
```
[Routing to external model: claude-opus-4-6 — reason: high-complexity reasoning task.
Note: your question text will leave the lab network. Raw data is never sent.
Type /local to force local model instead.]
```

**In the response:**
```
[Response generated by claude-opus-4-6 via external API]
<response text>
```

The `/local` override is always available and always honoured immediately.

**Audit log entry (every remote call):**
```json
{
  "timestamp": "...",
  "agent_id": "qtm",
  "user_id": "frank@icfo.eu",
  "model": "claude-opus-4-6",
  "trigger": "task_classification",
  "tokens_sent": 842,
  "tokens_received": 310,
  "data_minimization_check": "passed"
}
```

---

### Tasks

- [ ] Add `model_tier` and `remote_reason` fields to `AgentState` TypedDict
- [ ] Implement `ModelRouter` class with local/remote routing and daily cap enforcement
- [ ] Add `frontier_models` section to `triggers.yaml`; default `enabled: false`
- [ ] Implement task classifier (runs on Hermes locally): classify request as local-only or remote-eligible
- [ ] Implement sanitisation prompt and `CANNOT_SANITISE` detection (runs on local Hermes before sudo call)
- [ ] Implement `remote_llm_call.py` wrapper: runs as `qnoe-ai-remote` via sudo, applies blocklist as defence-in-depth, logs to audit trail, makes call
- [ ] Create `qnoe-ai-remote` OS account: no `/mnt/qnoe-data` mount, outbound to whitelisted hosts only
- [ ] Add `sudo` rule scoped to `remote_llm_call.py` only
- [ ] Add `config/data_minimization.yaml` with blocklist patterns (defined at implementation time)
- [ ] Implement user transparency messages (pre-call notice + response attribution)
- [ ] Implement `/local` override command
- [ ] Add remote call entries to audit log schema
- [ ] Test: `qnoe-ai-remote` cannot read `/mnt/qnoe-data`
- [ ] Test: `qnoe-ai` cannot reach `api.anthropic.com` directly
- [ ] Test: sanitisation prompt strips device IDs from a known context containing `SLG07`
- [ ] Test: `CANNOT_SANITISE` response aborts the remote call and falls back to local
- [ ] Test: blocklist catches a sanitisation failure that slipped through
- [ ] Test: daily cap enforced; exceeded cap falls back to local gracefully

### Acceptance criteria

- Agent process itself cannot make outbound API calls — only the wrapper can
- User receives a pre-call notice every time remote routing occurs
- A prompt containing a device identifier (e.g. "SLG07") is blocked by the data minimization check
- Daily request cap is enforced; exceeding it falls back to local with a logged message
- `/local` override works from any context and is honoured immediately

**Depends on:** Phase 1 complete · `AgentState` TypedDict implemented · Audit log operational

---

## B8 — Failing-notebook trigger (deferred MVP criterion #5, 2026-07-10)

Proactive detection of error outputs in notebooks: watcher/nightly sweep scans `.ipynb` cell outputs for
tracebacks in QTM + photocurrent repos; on a new failure, DM the repo owner (owner map: `config/maintainer.yaml`).
**Status:** never implemented — `config/triggers.yaml` does not exist and there is no trigger code; deferred out of
MVP-1 by user decision 2026-07-10 (see [[AGENT_FRAMEWORK]] §9.4 rescope note). Design source: [[AGENT_FRAMEWORK]] §7.
**Build notes:** the nightly pipeline + Teams channel/DM reporting infra ([[memory/agent-code#Nightly Report]])
already exist — implementation is a scanner + dedup state table + a `post_report.py`-style DM call.

## B9 — New-paper channel summaries (deferred MVP criterion #6, 2026-07-10)

When the nightly ingest indexes a new PDF in the literature store, post an LLM summary to the relevant sub-team
channel next morning. **Status:** ingestion + nightly report exist; per-paper summaries were never implemented;
deferred out of MVP-1 (2026-07-10). **Build notes:** hook into the nightly manifest diff (new `.pdf` rows),
summarise with the production model (gpt-oss-120b — cheap now at ~47 tok/s), reuse the channel-posting path from
`agent/reporting/post_report.py`. Needs `ChannelMessage.Send` on the right channels (see TODO I8 for permissions).

## B10 — Cross-team synthesis via orchestrator fan-out (deferred MVP criterion #10, 2026-07-10)

Orchestrator queries multiple sub-agents in parallel for cross-team questions and synthesises the answers.
**Status:** designed for LangGraph ([[AGENT_FRAMEWORK]] §5); under Hermes, routing is per-user and the `delegation`
toolset is deliberately disabled — no fan-out path exists. Deferred out of MVP-1 (2026-07-10).
**Build notes:** candidates are (a) re-enable Hermes delegation for the orchestrator profile only, or (b) a
`qnoe_synthesis` plugin tool that calls the other profiles' collections directly through `qnoe_rag` (cheaper: RAG
fan-out without a second agent). Decide when a real cross-team use case appears.
