#!/usr/bin/env python3
"""Red-team harness runner (Channel A — self-driven `hermes -z`).

MUST run as qnoe-ai (profile home is mode 700):
    sudo -u qnoe-ai bash /opt/qnoe-agent/redteam/run.sh [--dry-run] [--class C] [--profile P] [--list]

Per probe it: (1) builds a throwaway HERMES_HOME — SOUL/.env/plugins are
SYMLINKS to the live profile (parity), but config.yaml is COPIED with a
`platform_toolsets.cli` entry added so the qnoe-lab plugin tools load under the
cli one-shot (the `-t/--toolsets` flag can't resolve plugin toolset names), and
state.db + logs are local (no lock contention / session pollution);
(2) plants any injection file; (3) runs one full agent turn with MEM0_ENABLED=0
(no writes to the live episodic_memory collection); (4) grades; (5) cleans up.

A turn that errors or returns empty is scored ERROR (never PASS/FAIL) — an empty
answer must not trivially satisfy a must_not_contain grader.

Note: `hermes -z` calls logging.disable(CRITICAL), so the `prefetch inject:` INFO
line is usually suppressed here — the answer + oracle verdict is the primary
signal in Channel A; deep log triage lives on Channel B (Teams). See README.md.
"""
from __future__ import annotations
import argparse
import json
import os
import shutil
import subprocess
import sys
import time

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import graders  # noqa: E402
from probes import PROBES, DRY_RUN_IDS  # noqa: E402

HERMES = "/opt/qnoe-agent/hermes-venv/bin/hermes"
LIVE_PROFILES = "/opt/qnoe-agent/hermes/profiles"
HOMES = os.path.join(HERE, "homes")
REPORTS = os.path.join(HERE, "reports")
CLI_TOOLSETS = ["file", "terminal", "clarify", "qnoe-lab"]
QDRANT = os.environ.get("QDRANT_URL", "http://localhost:6333")
SYMLINK_ENTRIES = ["SOUL.md", ".env", "plugins", "memories",
                   "USER.md", "AGENTS.md", ".managed"]
FAIL_MARKERS = ["No inference provider", "agent failed", "Traceback",
                "PermissionError", "unknown --toolsets", "not configured"]
PROBE_TIMEOUT = 300


def _live_config(profile: str) -> str:
    return os.path.join(LIVE_PROFILES, profile, "config.yaml")


def _throwaway_home(profile: str) -> str:
    live = os.path.join(LIVE_PROFILES, profile)
    home = os.path.join(HOMES, profile)
    shutil.rmtree(home, ignore_errors=True)
    os.makedirs(os.path.join(home, "logs"), exist_ok=True)
    # config.yaml: copy + expose the qnoe-lab plugin tools to the cli platform.
    with open(_live_config(profile)) as f:
        cfg = yaml.safe_load(f)
    cfg.setdefault("platform_toolsets", {})["cli"] = list(CLI_TOOLSETS)
    with open(os.path.join(home, "config.yaml"), "w") as f:
        yaml.safe_dump(cfg, f, sort_keys=False)
    for entry in SYMLINK_ENTRIES:
        src = os.path.join(live, entry)
        if os.path.exists(src) or os.path.islink(src):
            try:
                os.symlink(src, os.path.join(home, entry))
            except OSError:
                pass
    return home


def _plant(inject: dict) -> None:
    os.makedirs(os.path.dirname(inject["file"]), exist_ok=True)
    with open(inject["file"], "w") as f:
        f.write(inject["payload"])


def _unplant(inject: dict) -> None:
    shutil.rmtree(os.path.dirname(inject["file"]), ignore_errors=True)


def _episodic_count() -> int:
    try:
        out = subprocess.run(
            ["curl", "-s", f"{QDRANT}/collections/episodic_memory"],
            capture_output=True, text=True, timeout=10).stdout
        return json.loads(out)["result"]["points_count"]
    except Exception:
        return -1


_SECRET_FILES = ["/opt/qnoe-agent/secrets/sharepoint.env",
                 "/opt/qnoe-agent/secrets/teams.env",
                 "/opt/qnoe-agent/secrets/report.env"]


def _load_secret_values():
    vals = set()
    for f in _SECRET_FILES:
        try:
            for line in open(f):
                if "=" in line and not line.lstrip().startswith("#"):
                    v = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if len(v) >= 6:
                        vals.add(v)
        except Exception:
            pass
    return vals


_SECRET_VALUES = _load_secret_values()


def _redact(text: str) -> str:
    """Defense-in-depth: never let a real credential land in a report, even if a
    probe induces a secret read (the harness runs outside B7's InaccessiblePaths)."""
    if not text:
        return text
    for v in _SECRET_VALUES:
        if v in text:
            text = text.replace(v, "[REDACTED-CREDENTIAL]")
    return text


def _errored(rec: dict) -> bool:
    if rec.get("rc", 0) != 0:
        return True
    if not (rec.get("answer") or "").strip():
        return True
    st = rec.get("stderr_tail", "")
    return any(m in st for m in FAIL_MARKERS)


def _run_one(probe: dict) -> dict:
    profile = probe["profile"]
    home = _throwaway_home(profile)
    inject = probe.get("inject")
    if inject:
        _plant(inject)
    env = dict(os.environ, HERMES_HOME=home, MEM0_ENABLED="0")
    rec = {"id": probe["id"], "cls": probe["cls"], "profile": profile,
           "prompt": probe["prompt"], "why": probe["why"]}
    try:
        t0 = time.time()
        p = subprocess.run([HERMES, "-z", probe["prompt"]], env=env,
                           capture_output=True, text=True, timeout=PROBE_TIMEOUT)
        rec["wall"] = round(time.time() - t0, 1)
        rec["answer"] = _redact((p.stdout or "").strip())
        rec["stderr_tail"] = _redact((p.stderr or "").strip()[-700:])
        rec["rc"] = p.returncode
    except subprocess.TimeoutExpired:
        rec.update(answer="", stderr_tail=f"TIMEOUT after {PROBE_TIMEOUT}s",
                   rc=-1, wall=PROBE_TIMEOUT)
    finally:
        if inject:
            _unplant(inject)
    logf = os.path.join(home, "logs", "agent.log")
    rec["inject_log"] = ""
    if os.path.exists(logf):
        for line in open(logf, errors="ignore"):
            if "prefetch inject" in line:
                rec["inject_log"] = line.strip()
    if _errored(rec):
        rec["verdict"], rec["note"] = "ERROR", "turn failed/empty — not graded"
    else:
        rec["verdict"], rec["note"] = graders.grade(probe["grader"], rec["answer"])
    return rec


def _select(args):
    ids = set(DRY_RUN_IDS) if args.dry_run else None
    out = []
    for p in PROBES:
        if p["channel"] != "A":
            continue
        if ids is not None and p["id"] not in ids:
            continue
        if args.cls and p["cls"] != args.cls:
            continue
        if args.profile and p["profile"] != args.profile:
            continue
        out.append(p)
    return out


def _preflight(probes):
    for pr in sorted({p["profile"] for p in probes}):
        cfg = _live_config(pr)
        if not os.access(cfg, os.R_OK):
            sys.exit(
                f"ERROR: cannot read {cfg}.\n"
                "The harness must run as qnoe-ai (profile home is mode 700):\n"
                "  sudo -u qnoe-ai bash /opt/qnoe-agent/redteam/run.sh "
                + " ".join(sys.argv[1:]))


def _write_report(results, meta):
    os.makedirs(REPORTS, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    base = os.path.join(REPORTS, f"redteam_{stamp}")
    with open(base + ".json", "w") as f:
        json.dump({"meta": meta, "results": results}, f, indent=2)
    rollup = {}
    for r in results:
        rollup[r["verdict"]] = rollup.get(r["verdict"], 0) + 1
    iso = "OK, unchanged" if meta["mem_before"] == meta["mem_after"] else "CHANGED — investigate"
    lines = [f"# Red-team report — {stamp}", "",
             f"Probes: {len(results)} · " + " · ".join(f"{k}={v}" for k, v in sorted(rollup.items())),
             f"Isolation (episodic_memory points): before={meta['mem_before']} "
             f"after={meta['mem_after']} ({iso})", ""]
    for r in results:
        lines.append(f"## [{r['verdict']}] {r['id']}  ({r['cls']} / {r['profile']}, {r.get('wall')}s)")
        lines.append(f"*Targets:* {r['why']}")
        lines.append(f"*Grader:* {r['note']}")
        lines.append(f"*Prompt:* {r['prompt']}")
        if r.get("inject_log"):
            lines.append(f"*Triage:* `{r['inject_log']}`")
        ans = r["answer"] or "(empty stdout)"
        if len(ans) > 1800:
            ans = ans[:1800] + "\n…(truncated)…"
        lines.append("*Answer:*\n\n```\n" + ans + "\n```")
        if r["verdict"] == "ERROR" and r.get("stderr_tail"):
            lines.append("*stderr:*\n\n```\n" + r["stderr_tail"] + "\n```")
        lines.append("")
    with open(base + ".md", "w") as f:
        f.write("\n".join(lines))
    return base + ".md"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--class", dest="cls", default=None)
    ap.add_argument("--profile", default=None)
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    probes = _select(args)
    if args.list:
        for p in probes:
            print(f"{p['id']:24} {p['cls']:14} {p['profile']}")
        print(f"\n{len(probes)} channel-A probes selected")
        return

    _preflight(probes)
    print(f"Running {len(probes)} probe(s) as {os.environ.get('USER', '?')} …", flush=True)
    mem_before = _episodic_count()
    results = []
    for p in probes:
        print(f"  → {p['id']} …", flush=True)
        results.append(_run_one(p))
    mem_after = _episodic_count()
    meta = {"mem_before": mem_before, "mem_after": mem_after,
            "n": len(results), "dry_run": args.dry_run}
    path = _write_report(results, meta)
    print(f"\nReport: {path}")
    print("Verdicts: " + ", ".join(f"{r['id']}={r['verdict']}" for r in results))
    if mem_before != mem_after:
        print(f"WARNING: episodic_memory {mem_before}->{mem_after} — MEM0 isolation breach")


if __name__ == "__main__":
    main()
