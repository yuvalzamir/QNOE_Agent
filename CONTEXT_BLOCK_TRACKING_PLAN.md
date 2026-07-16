# Context-Block Tracking + Regular Review — Implementation Plan

*Written: 2026-07-15 · Status: **EXECUTED + DEPLOYED 2026-07-16** (see [[TODO]] ✅ item + [[memory/agent-code#Context-block tally]]) · Source: TODO.md line 32 (user request 2026-07-15)*

> Execution deltas vs this plan: (1) MEMORY/USER live at `profiles/<p>/memories/` and are scanned PER-ENTRY at **strict** scope by `tools.memory_tool` (second warning format: `Memory entry from <name> blocked at load time: <ids>`) — `soul_health.py` was rewritten to mirror both surfaces; (2) RAG chunks/tool results are NOT scanned by this Hermes core — no tracking gap; (3) the post_report "wiring mystery" resolved benignly: it's a separate 07:00 cron line, no live-only edit existed; (4) event kinds are `context_file`/`memory_entry`/`anomaly` (no `rag`/`tool_result`).

> Hand-off plan in the style of [[CONTEXT_EXECUTION_PLAN]] / [[GPT_OSS_CUTOVER_PLAN]]. Everything here is design — no code deployed yet.

## Context

Hermes' threat-scanner silently drops entire context files from the prompt when a regex matches (`scan_for_threats(text, "context")` in `prompt_builder`). M53 showed the cost: the orchestrator ran ~18h with **no SOUL** and the only trace was a per-turn WARNING in a 0700 qnoe-ai-only log (`agent.prompt_builder: Context file SOUL.md blocked: prompt_injection, deception_hide`). SOUL.md is now core-whitelisted, so the live risk is **MEMORY.md / USER.md + tool-result/RAG blocks**.

TODO.md line 32 asks for: **(a)** parse the per-turn blocked-WARNINGs from every profile's `agent.log` into a running tally (file/pattern/when), **(b)** surface a line in the nightly Teams report, **(c)** a periodic qnoe-ai scan so live file edits are caught between gateway restarts. Goal: **no context content is ever silently dropped without someone seeing it.**

Seed that exists today: `scripts/soul_health.py` (static scan of SOUL/MEMORY/USER using Hermes' own scanner) runs **only at gateway startup** via `scripts/start_hermes.sh:83-92`, writing `logs/soul_health.json` + a `[startup]` line.

**Decisions taken** (user was AFK when asked; recommended options chosen, both easily changeable):
- **Scheduler: systemd timer**, not qnoe-ai crontab — `sudo crontab -u qnoe-ai` isn't in the NOPASSWD set (user action, untestable by Claude), while a `User=qnoe-ai` oneshot unit + hourly timer deploys/tests/rolls back entirely with allowed sudo (`cp`/`systemctl`) and matches the `qnoe-b7-test.service` pattern. Crontab fallback line to be documented in docs.
- **Alerting: nightly report only** (TODO scope = "regular review"). Immediate per-block Teams alerts would require giving the out-of-sandbox qnoe-ai job the Graph credentials — noted as a future option, not built.

## Architecture

```
qnoe-context-tally.timer (hourly, User=qnoe-ai, OUTSIDE sandbox)
  └─ scripts/context_block_tally.py
       1. incremental-parse hermes/profiles/*/logs/agent.log (0700 qnoe-ai — only qnoe-ai can)
          → append events to logs/context_blocks.jsonl        (group-readable, 30-day self-prune)
       2. subprocess soul_health.py --json (fresh static scan, catches live edits between restarts)
          → atomic rewrite of logs/soul_health.json
       3. update logs/context_block_tally.state.json (offsets/inodes/last_run)

nightly cron 02:00 (yzamir) — nightly_run.py
  └─ NEW task_context_blocks(): READS the three qnoe-ai-written files in logs/
       → 24h summary by profile/file/pattern + static-scan status + tally-staleness check
  └─ post_report.py renders the line in the Teams "Agent Logs" report
```

Why this shape: respects the UID boundary (yzamir cannot read profile logs), no new Hermes core patches (M53 — site-packages patches silently revert on upgrade), no cross-UID SQLite (M52 WAL trap — append-only JSONL, single writer per file). Log-parsing is kept **in addition to** re-scanning because the log is the only record of what was actually dropped at turn time (tool-result/RAG blocks, transient edit→block→revert states).

Three safeguards designed in:
1. **The monitor monitors itself** — `task_context_blocks()` flags `tally: STALE` if the state file's `last_run` is >3h old, and treats missing/unreadable inputs as a task FAILURE, never as "clean".
2. **Format-drift canary** — parser runs a strict regex (extracts file + pattern ids) AND a loose detector (`prompt_builder` + `block`); loose-but-not-strict lines become `anomaly` events surfaced in the report, so a Hermes upgrade changing the message text becomes visible instead of silently zeroing the tally.
3. **Atomic writes, tolerant reads** — tmp + `os.replace()` for JSON rewrites, chmod 0664 (yzamir reads via qnoe-ai group); nightly reader skips an unparseable trailing JSONL line.

## Phase A — Live verification on DGX (read-only, before final code)

Needs SSH (ask at session start per rule). All via NOPASSWD `sudo cat`/`ls`:

1. **Enumerate ALL block-warning formats** in live core: `sudo cat .../hermes-venv/.../site-packages/agent/prompt_builder.py` — every warning around `_scan_context_content` and any tool-result/RAG scan site. Fixes the strict regex set + event `kind` enum (`context_file|tool_result|rag|anomaly`). If core logs nothing for tool-result/RAG blocks, only context-file blocks are trackable — document the gap, do NOT patch core.
2. **agent.log line format + rotation**: `sudo cat` tail of `hermes/profiles/qnoe-orchestrator/logs/agent.log`; `sudo ls -la profiles/*/logs/` (rotated siblings? timestamps per line?). Decides real `ts` vs ingestion-time fallback.
3. **M50 drift check** — diff live vs repo (compare against `git show HEAD:<file>`, LF) for `nightly_run.py`, `post_report.py`, `soul_health.py`, `start_hermes.sh`. **Specifically resolve how post_report is invoked today**: repo `nightly_run.py` never calls it and the documented crontab doesn't chain it, yet docs say it's wired (M50-class live-only edit suspected). Capture any live-only edits into the repo + commit BEFORE layering changes. Also `crontab -l` as yzamir.
4. Confirm `logs/` perms + owner/mode of existing `logs/soul_health.json` (yzamir must be able to read it).

## Phase B — Changes (branch `feature/context-block-tally`)

### B1. NEW `scripts/context_block_tally.py`
Mirrors `soul_health.py` conventions (env-overridable `HERMES_ROOT`/`HERMES_VENV_SITE_PACKAGES`, stdlib-only). Per run:
- **Parse**: for each `profiles/<p>/logs/agent.log` (+ rotated siblings): stat; inode unchanged & size ≥ offset → read from offset; rotation/truncation → drain old inode via rotated filename then read new from 0. Dedup belt-and-braces: hash `(profile, kind, file, patterns, ts_or_line)` against last ~500 `recent_hashes` in state. Strict regexes → events; loose-only lines → `anomaly` events (raw line truncated ~300 chars).
- **Event schema** (one JSON/line in `logs/context_blocks.jsonl`, O_APPEND, umask 0002):
  `{"ts": <log ts or null>, "ingested_at": <iso utc>, "profile", "kind", "file", "patterns": [...], "raw"}`
- **Rescan**: subprocess `soul_health.py --json` (same interpreter; reuses its output contract, no import coupling) → atomic write `logs/soul_health.json` (0664). `start_hermes.sh` startup write stays as-is.
- **Prune**: rewrite JSONL keeping <30 days (sp_activity precedent), atomic; state file written last.
- Partial errors never abort later steps; exit 0, errors to stderr → journal.

### B2. NEW systemd units (repo copies under `scripts/`, deployed to `/etc/systemd/system/`)
- `qnoe-context-tally.service`: `Type=oneshot`, `User=qnoe-ai`, `Group=qnoe-ai`, `UMask=0002`, `WorkingDirectory=/opt/qnoe-agent`, env for HERMES_ROOT/SITE_PACKAGES, `ExecStart=/opt/qnoe-agent/hermes-venv/bin/python3 /opt/qnoe-agent/scripts/context_block_tally.py`. **No sandbox wrapper** — it must read the 0700 profile logs as owner; scope = read `hermes/`, write `logs/` (comment in unit).
- `qnoe-context-tally.timer`: `OnCalendar=*-*-* *:17:00` (clear of the 02:00 nightly), `Persistent=true`.
- Crontab fallback line documented in RUNBOOK for if the user ever prefers cron.

### B3. `agent/indexing/nightly_run.py` — new `task_context_blocks()`
Reads (never writes) the three logs/ files. Returns:
```python
{"window_hours": 24, "events": N, "anomalies": N,
 "by_target": {"qnoe-qtm/MEMORY.md": {"deception_hide": 2}},
 "kinds": {"context_file": N, "tool_result": N},
 "static_scan": {"summary": str, "blocked": N, "scanned": N, "age_hours": f},
 "tally_last_run": iso, "tally_stale": bool}
```
Window filter on `ts` falling back to `ingested_at`. Unreadable soul_health.json → `static_scan: {"error": ...}` + `logger.warning` (auto-surfaces in report WARNINGS). Raise only if BOTH sources missing (task FAIL = monitor is down, correct). Register in `TASKS` (~line 434), `TASK_TIMEOUTS` (5 min), `_summarise_stats` branch (~652–706): `blocks 24h: 2 — qnoe-qtm/MEMORY.md (deception_hide ×2) | static: CLEAN (12 files) | tally 0.4h ago`, plus `⚠ TALLY STALE` / `⚠ N unparsed block-lines` variants.

### B4. `agent/reporting/post_report.py` — `_task_detail` branch (~140–195)
HTML mirror of the txt line, following the SharePoint poller-dropped precedent (lines 166–185); bold alarming bits; cap listed targets at 5 with `+N more`.

### B5. Docs (same commit)
- `TODO.md:32` → checked with summary; update the "STILL OPEN" note at line 36 (nightly surfacing now done).
- `memory/agent-code.md` — "Context-block tally" section (files, schema, units).
- `memory/deploy-patterns.md` — new units + **tally regexes depend on core log text: re-verify after any Hermes upgrade** (next to the "Hermes core patches" table).
- `memory/infrastructure.md` / RUNBOOK — timer, the three logs/ artifacts, crontab fallback.

## Phase C — Deployment order (M50-safe)

1. Phase A recon; commit any captured live-only edits first.
2. Implement B1–B5 on the branch; deploy from `git show HEAD:<file>` (LF), never the CRLF working tree.
3. Deploy `context_block_tally.py`: `/tmp` → `sudo cp` → `sudo chown qnoe-ai:qnoe-ai` → `sudo chmod 775`.
4. Deploy units → `sudo systemctl daemon-reload` → `sudo systemctl enable --now qnoe-context-tally.timer`.
5. First run: `sudo systemctl start qnoe-context-tally.service`; verify outputs (`journalctl` not NOPASSWD — rely on `sudo cat` of the output files); verify all three logs/ files readable **as yzamir** (nightly's hard requirement). Expect historical M53 SOUL-block events if those log lines survive.
6. Diff-then-deploy `nightly_run.py` + `post_report.py` (backup live to `.bak-pre-tally` via `sudo cp` first).
7. Test as yzamir with cron env: `python -m agent.indexing.nightly_run` task path + `post_report` dry-run → new line renders.
8. No gateway restart needed (nothing in the gateway changed).

## Phase D — End-to-end test (planted trigger)

1. Least-used profile (qnoe-photocurrent): `sudo cp MEMORY.md MEMORY.md.bak-tallytest`, plant a known trigger line (`ignore previous instructions`).
2. `sudo systemctl start qnoe-context-tally.service` → `soul_health.json` shows the block (**static path = "live edit caught between restarts" proven, no gateway restart**).
3. User sends one Teams message to that profile → per-turn WARNING → run service again → `context_file` event in `context_blocks.jsonl` (**log-parse path proven**).
4. Nightly task + post_report dry-run show `blocks 24h: 1 …`.
5. Revert MEMORY.md from backup, re-run → static CLEAN; historical event correctly remains in tally. Remove .bak.
6. Idempotency: run service twice back-to-back → event count unchanged (offsets/dedup working).
7. Next morning: confirm the line in the Teams "Agent Logs" nightly report. Commit everything deployed; tick TODO.

## Rollback

- `sudo systemctl disable --now qnoe-context-tally.timer`; units overwritable with `/dev/null` via `sudo cp` (no sudo rm).
- Restore `nightly_run.py`/`post_report.py` from `.bak-pre-tally`. Task also fails soft — runner isolates task exceptions (one FAIL row, other tasks unaffected).
- `context_blocks.jsonl` / state file are inert data.
- Nothing in site-packages → nothing a Hermes upgrade silently reverts; the only coupling is the WARNING text, guarded by the anomaly canary.

## Accepted residual gaps
- Blocks whose log lines are destroyed by rotation before the hourly read could be missed (rotated-file drain makes this near-zero at current volume).
- If core logs nothing for tool-result/RAG blocks, those stay untracked (documented, not patched).
- `ts` may be ingestion-time (≤1h skew) if agent.log lines are timestamp-less — fine for daily review.
