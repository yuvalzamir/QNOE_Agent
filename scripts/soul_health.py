#!/usr/bin/env python3
"""SOUL / context-file health check.

Hermes' prompt_builder runs `tools.threat_patterns.scan_for_threats(text,
"context")` over every context file it loads (SOUL.md, MEMORY.md, USER.md,
tool results). If a file matches ANY pattern the WHOLE file is dropped from the
prompt and only a per-turn WARNING is logged in that profile's agent.log — so a
SOUL can silently stop applying for days (see memory/mistakes.md M53: the
orchestrator ran ~18h with no SOUL because its SharePoint rule matched the
`deception_hide` regex).

This check scans every profile's context files with Hermes' OWN scanner (so it
can never drift from production) and reports any file that would be blocked,
with the offending line numbers. Exit code 1 if any file is blocked, 0 if all
load. `--json` emits a machine-readable summary for the nightly report.

Run: /opt/qnoe-agent/hermes-venv/bin/python3 scripts/soul_health.py
(also works under the agent venv — it locates the hermes-venv scanner itself).
"""
import glob
import json
import os
import sys

HERMES_ROOT = os.environ.get("HERMES_ROOT", "/opt/qnoe-agent/hermes")
HERMES_VENV_SP = os.environ.get(
    "HERMES_VENV_SITE_PACKAGES",
    "/opt/qnoe-agent/hermes-venv/lib/python3.12/site-packages",
)
CONTEXT_FILES = ["SOUL.md", "MEMORY.md", "USER.md"]
# Mirror the prompt_builder whitelist (QNOE core patch, mistakes M53): SOUL.md is
# operator-authored and exempt from blocking, so it can never be silently dropped.
# MEMORY.md / USER.md stay scanned (they can carry poisoned/distilled content), so
# THEY are the files a silent drop can now hit — that's what this monitor guards.
EXEMPT = {"SOUL.md"}


def _load_scanner():
    """Return Hermes' real scan_for_threats, importing from hermes-venv if needed."""
    try:
        from tools.threat_patterns import scan_for_threats
    except ImportError:
        if HERMES_VENV_SP not in sys.path:
            sys.path.insert(0, HERMES_VENV_SP)
        from tools.threat_patterns import scan_for_threats
    return scan_for_threats


def _offending_lines(text, scan):
    """Best-effort per-line attribution (a cross-line match won't be pinned)."""
    return [i for i, line in enumerate(text.splitlines(), 1) if scan(line, "context")]


def scan_all():
    scan = _load_scanner()
    results = []
    for prof_dir in sorted(glob.glob(os.path.join(HERMES_ROOT, "profiles", "*"))):
        if not os.path.isdir(prof_dir):
            continue
        prof = os.path.basename(prof_dir)
        for cf in CONTEXT_FILES:
            path = os.path.join(prof_dir, cf)
            if not os.path.isfile(path):
                continue
            text = open(path, encoding="utf-8", errors="replace").read()
            ids = scan(text, "context")
            exempt = cf in EXEMPT
            entry = {"profile": prof, "file": cf, "exempt": exempt,
                     "blocked_by": [] if exempt else ids,
                     "would_match": ids if exempt else []}
            if entry["blocked_by"]:
                entry["lines"] = _offending_lines(text, scan)
            results.append(entry)
    return results


def summary_line(results):
    """One-line status suitable for the nightly report."""
    blocked = [r for r in results if r["blocked_by"]]
    if not blocked:
        fyi = [r for r in results if r.get("would_match")]
        base = f"SOUL health: {len(results)} context files, all load ✅"
        return base + (f" (FYI: {len(fyi)} exempt file(s) contain scanner-trigger text)" if fyi else "")
    parts = [f"{r['profile']}/{r['file']} ({','.join(r['blocked_by'])})" for r in blocked]
    return f"SOUL health: ⚠️ {len(blocked)}/{len(results)} BLOCKED — " + "; ".join(parts)


def main(argv):
    results = scan_all()
    blocked = [r for r in results if r["blocked_by"]]
    if "--json" in argv:
        print(json.dumps({"blocked": blocked, "scanned": len(results),
                          "summary": summary_line(results)}))
    elif "--line" in argv:
        print(summary_line(results))
    else:
        print(f"SOUL/context-file health: scanned {len(results)} file(s)")
        if not blocked:
            print("ALL CLEAN — every context file loads.")
        else:
            print(f"WARNING: {len(blocked)} file(s) BLOCKED "
                  "(dropped from the prompt — their rules are NOT applied):")
            for r in blocked:
                ln = ",".join(map(str, r.get("lines", []))) or "?"
                print(f"  - {r['profile']}/{r['file']}: {r['blocked_by']} (line {ln})")
            print("Fix: reword the offending line so it stops matching "
                  "tools/threat_patterns.py, then it reloads on the next turn.")
    return 1 if blocked else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
