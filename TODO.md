# QNOE Lab Agent — Master TODO
*Last updated: 2026-07-20 — cleaned: completed history removed (it lives in git history, [[SETUP_LOG]] and the vault); KùzuDB L5 section deleted (superseded by Cognee, [[memory/decisions#D20]]).*

> Claude Code memory: [[HOME]] · Decisions: [[memory/decisions]] · Mistakes: [[memory/mistakes]]
> Status: MVP-1 declared 2026-07-10 (gpt-oss-120b via llama.cpp, 3 Hermes profiles, B7-OS sandbox, hybrid RAG + Mem0 + registry grounding, T0/T1 read-only).

---

## In flight

- [~] **🔴 Cognee corpus knowledge-graph (L5) — HIGH-EFFORT PILOT RUN IN FLIGHT (2026-07-20).** Design: [[COGNEE_PLAN]] · [[COGNEE_ONTOLOGY]] · [[KG_ONEPAGER]] (Frank) · decision [[memory/decisions#D20]].
  - **Run state (MEDIUM run, launched ~14:00 local, PID 81088):** **effort:high abandoned — VERDICT: non-convergent on this workload.** Two high attempts, two distinct failure modes, zero successful extractions: run #1 starved by litellm's 600s default client timeout (calls cut at ~10.5K tokens = exactly 600s × 18 tok/s; diagnosed via cognee_db `session_model_usage`=0 despite 50 server-side "completions" — those were disconnect-aborts); run #2, with the timeout fixed (b05aecb, `llm_args={"timeout":3600}`), revealed the true behavior: generations ran past **17.6K tokens with no sign of terminating** (the 16,384 `max_completion_tokens` isn't enforced on this adapter path). gpt-oss at high effort simply doesn't converge on the ontology-extraction prompt → even best-case ≈ 20 min/call ≈ 48h+/run. **User decision: medium** (~2-4K reasoning/call, ETA ~7-10h). Monitor emits "CONVERGENCE CONFIRMED" on the first successful extraction — if medium ALSO fails to converge, the conclusion is a dedicated extraction model / smaller per-call schema, not more effort. High-run logs: `overnight.log.jul20-starved`, `.jul20-high2`. **⚠️ After the run: restore effort low** (`sudo systemctl unset-environment LLAMA_REASONING_EFFORT && sudo systemctl restart vllm.service`).
  - **⚠️ LLM is serving at reasoning_effort HIGH for the duration** (`LLAMA_REASONING_EFFORT=high` via `systemctl set-environment`; `start_llamacpp.sh` parameterized, default low, backup `.bak-pre-effort`). ALL agent traffic is slower meanwhile. **RESTORE AFTER THE RUN: `sudo systemctl unset-environment LLAMA_REASONING_EFFORT && sudo systemctl restart vllm.service`.**
  - [ ] **Judge gate (USER):** when done, judge `output/qtm_full.md` vs the worked subgraph in [[COGNEE_ONTOLOGY]] §4 — *sensible & non-confabulated?* Go/no-go for the whole Tier-2 conceptual layer (if it confabulates → dedicated extraction model).
  - [ ] After the gate: **Phase 1 — LLM-free registry backbone** via `add_data_points` ([[COGNEE_PLAN]]).
  - **Design requirement (carried from the deleted Kùzu section — the 2026-07-10 BSCCO gap):** the entity graph must be **group-visible from every profile** (material → sub-team → runs → setups) while document *content* stays profile-scoped, so a QTM user asking about another team's material gets a pointer, not silence or confabulation. Related: [[PHASE2_BACKLOG]] B10.

- [~] **🟡 Tuning window open (since 2026-07-20):** `QNOE_GROUNDING_CHECK_SAMPLE_PARAMS=1` + `MEM0_WRITE_GATE=1` in `start_hermes.sh` (backup `.bak-pre-tuning`). Write-gate live-verified same day (3 correct DROPs, 1 KEEP w/ provenance; YBCO collective-subject miss fixed — commit c8d2c95, planted fact purged). **Next:** after a few days of logs, review `missample=`/`misparam=` FPs + `memory_gate KEEP/DROP` lines → bake both as code defaults + sync repo `start_hermes.sh`, or adjust. Rollback: env flip.

- [ ] **Verify tomorrow's nightly (first run with 3 changes, 2026-07-21 morning):** (a) `task_server_sweep` — `new_files` count + failed batches; (b) `task_server_coverage` line present; (c) SP coverage — `General` should CLEAR the <80% flag now the plot-class is excluded. **Watch over following nights:** sweep `new_files` should fall toward 0 — persistently high = permanent-skip files re-attempted nightly → add manifest skip-tombstones.

## User actions

- [ ] **Redteam R11 close-out:** `sudo -u qnoe-ai bash /opt/qnoe-agent/redteam/run.sh --class survey-confab` ×3 → `survey-fake-run-in-list` + `survey-misattribution` carry the run↔DB/run↔type ⚠️ footer (or the model abstains); `survey-real-baseline` stays clean. (Misattribution validator deployed 2026-07-20; DGX offline suites 5/5.) Deferred R11 options: opt 5 registry-hook survey phrasing, opt 4 gated CoVe — see `redteam/BACKLOG.md`.
- [ ] **Teams HTML formatting — desktop check** (phone render was poor 2026-07-20; Teams mobile renders a reduced HTML subset, desktop is the real test). Deployed 0fe41a9.
- [ ] **Forward the web-access draft to Frank** ([[memory/decisions#D21]] #6 — toolsets stay off meanwhile): *"The agent currently answers only from lab-internal sources — nothing leaves the network. We could enable web search so it can pull recent literature/docs, at the cost of query text (not documents) going to external search engines. Recommend deciding per Phase-2 when we also discuss frontier-model access (B4)."*
- [ ] **PPTX Gantt** still reflects the old phase order — update before the next PI presentation.

## Near-term (agent)

- [ ] **Restore `reasoning_effort:low`** after the Cognee run (command above; flagged in the monitor's completion event).
- [ ] **Mem0 hygiene remainder** ([[MEM0_HYGIENE_OPTIONS]] option 4): a periodic/nightly audit that oracle-checks stored facts (declarative lab-claims, run/db/param assertions vs registry) and lists suspects + a purge-by-filter ops script (prototype `/tmp/purge_querylogs.py`; provenance metadata now enables surgical deletes). Provenance (#1) + write-gate (#2) + output oracle (#3) are live. Deferred to Cognee: own-work fact storage, LLM party-signal, SOUL memory-guard relaxation.
- [ ] **Idle-box Qdrant cross-check** (SP audit leftover — manifest-vs-points divergence check timed out under ingest load; re-run when the box is quiet).
- [ ] **DGX cleanup (~238 GB, when convenient):** unused vLLM-format weights `models/gpt-oss-120b/` (113 GB) + `/home/yzamir/{gpt-oss-120b-gguf,llama.cpp,provence_dl}` copies (~125 GB) — all superseded.

## Backlog

- [ ] **SP delta-sync design fixes (M47 root causes — the sweep/audit now *catch* what these drop, but the causes remain):** (a) fail-once=fail-forever — `_save_delta_link` advances the Graph delta token past failures; add a retry queue; (b) Docling `ProcessPoolExecutor` crash on back-to-back conversions silently skips the rest of a batch — recreate pool per file or a dedicated long-lived worker.
- [ ] **Confirm `ingest_sp_qcodes.py` ran** (one-time ingest of SP-hosted QCoDeS `.db` files into `qcodes-runs`; run-once flag — check before running).
- [ ] **I5c — verify SP delta sync appears in the nightly log** (poller-activity line shipped 2026-07-13; confirm it renders in a current report).
- [ ] **I8 — Teams channel @mentions:** blocked on IT granting `ChannelMessage.Read.All` to app `108a03c5`; then poll channel messages in `teams_polling`.
- [ ] **B7-OS Stage-7 optional hardening (pull only when its trigger appears):** dedicated sandbox uid (trigger: breach-containment concern / Phase-2 write tiers) · OpenShell inference proxy (trigger: remote/frontier model, B4) · retire the systemd drop-in rollback (trigger: weeks of stable soak; keeping it = free rollback) · tmpfs `/tmp` + read-only rootfs (skippable).
- [ ] **L4 skill registry** (Phase 3): skill format spec + loader; port Nbandstructure, then GRASP-TWINS.
- [ ] **Standing watches:** gpt-oss quirks in daily use (empty replies → raise `max_tokens` 4096→8192; prose tool-syntax → check `tool_use_enforcement`) · re-run the `inject-readme` probe periodically (probabilistic, not one-and-done) · context-block tally ride-along: confirm the first real `memory_entry` event parses · dedicated photocurrent-USER Teams round-trip (nice-to-have).
- [ ] **Benchmark re-run** — stale (written for Hermes-3); only if a fresh full-stack baseline is wanted; rubric + baseline in `benchmark/`.

## Phase 2 (by design — after the Cognee gate + PI decisions)

- T2–T4 write tiers: permission enforcement, Teams approval flow, soft-delete wrapper, full audit-log path.
- Web/search toolsets + frontier-model access (B4) — pending Frank.
- Remaining sub-team agents beyond orchestrator/QTM/photocurrent.
- Backlog features: [[PHASE2_BACKLOG]] (B-items).

---

*Completed work up to 2026-07-20 (context-pressure package, gpt-oss cutover, Hermes migration M1–M8, B7-OS sandbox, BM25 hybrid, Mem0, full server re-ingest M58, SP coverage audit + remediation, R11 grounding stack, per-user routing, context-block tally, D21 decision round) is recorded in git history, [[SETUP_LOG]], and the vault ([[memory/decisions]], [[memory/mistakes]], [[memory/ingestion]], [[memory/infrastructure]], [[memory/agent-code]]).*
