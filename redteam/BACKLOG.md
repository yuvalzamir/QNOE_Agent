# Red-team findings backlog

The loop's memory: each round's findings → root-cause → fix → re-verify status.
Newest round on top. Probe classes and the historical defects they target live
in `probes.py` and `memory/mistakes.md` (M37–M46).

## Round 1 — dry-run + first finding (2026-07-14)

Harness validated: `hermes -z` produces faithful full-agent turns (registry hook
fires headless, RAG loads, injection defense works), MEM0 isolation held (69→69).
Two harness bugs found + fixed en route: false-PASS on empty answers (empty turns
now score ERROR); brittle literal grader (`contains_any` added). Note: qnoe-lab
tools are DEFERRED behind Tool Search under `-z` (as in production) — the model
must `tool_search` to reach `qcodes_search`; diag probe re-scoped to check the
bridges are present.

### R1-tool-selection — "latest X sweep in setup Y" answered from RAG, not the tool
- Probe: `tool-last-gatesweep` (tool/qnoe-qtm) · Verdict: FAIL
- Symptom: asked for the most-recent gate sweep in the L110 QTM setup, the agent
  returned **run 13 in `xueyiao3_03.db`** (wrong device/path) with fabricated-looking
  param list, ignoring the setup filter and time-ordering. Oracle truth: **run 848**
  (Tip5Sample9, swept=gate, 2026-05-19).
- Root cause: the model answers from the injected RAG context (not time-ordered /
  setup-scoped) instead of calling the deferred `qcodes_search` tool with
  `path`+`swept_parameter`. The earlier fix added those tool filters but never made
  the model *choose* the tool. Reproduces in production (same deferred-tool config).
- Fix (2026-07-14): SOUL rule in all 3 profiles — for latest/last/recent or "X sweep
  in a named setup/DB" questions, MUST call `qcodes_search` (via tool_search) with
  path/swept_parameter, never answer from RAG. Re-verify: re-run probe → expect 848;
  then confirm on Teams (Channel B).

<!-- Template for a finding:
### R<n>-<id> — <one-line defect>
- Probe: <id> (<class>)  · Verdict: FAIL
- Symptom: <what the agent did>
- Root cause: <layer — RAG / hook / SOUL / Mem0 / tool>
- Fix: <commit / SOUL edit / config>  · Re-verify: PASS in R<n+1>
-->
