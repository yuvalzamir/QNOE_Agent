"""Regression tests for the Mem0 write-gate classifier (memory_gate.py).

Pure stdlib (no registry, no LLM) — runs anywhere. The KEEP corpus is calibrated
to REAL Mem0 extraction phrasing (subject-less predicates from the installed
FACT_RETRIEVAL_PROMPT + real-world outputs) so we don't over-drop genuine facts;
the DROP corpus is the M55 poisoning classes (lab records, lab query-logs,
third-party work).
"""
import sys
from _util import load_memory_gate

gate = load_memory_gate()
fails = []


def expect(fact, want_verdict):
    verdict, reason = gate.classify_fact(fact)
    ok = verdict == want_verdict
    print(f"[{'PASS' if ok else 'FAIL'}] want={want_verdict:4} got={verdict:4} "
          f"({reason}): {fact!r}")
    if not ok:
        fails.append(fact)


# ---------------------------------------------------------------- KEEP --------
# Canonical Mem0 phrasing (subject-less predicates) — generic personalization.
expect("Name is John", "keep")
expect("Is a Software engineer", "keep")
expect("Prefers dark theme", "keep")
expect("Prefers answers in bullet points", "keep")
expect("Favourite movies are Inception and Interstellar", "keep")
expect("Looking for a restaurant in San Francisco", "keep")   # non-lab lookup — harmless
expect("Had a meeting with John at 3pm", "keep")              # name mentioned, user's activity
expect("Discussed the new project", "keep")
# Lab FIRST-PARTY focus / interest POINTERS (kept — Mem0 is user-centric, so
# "Sample is X" means the user's sample).
expect("Sample is Tip5Sample9", "keep")
expect("Is working on gate sweeps", "keep")
expect("Interested in BLG cavity-QED physics", "keep")
expect("Is a member of the QTM subteam", "keep")
expect("Studying twisted bilayer graphene", "keep")
expect("Currently focused on photocurrent measurements", "keep")

# ---------------------------------------------------------------- DROP --------
# Lab RECORDS (a run id / .db / qcodes) — registry/Cognee territory.
expect("Run 848 measured 1.2 nA", "drop")
expect("Gate sweep run 118 is in Tip6Sample9", "drop")
expect("Asked about run 999999", "drop")
expect("Database D4_IV_measurements.db has 120 runs", "drop")
expect("Is working on run 848 analysis", "drop")             # own-work but a RECORD (interim)
# Lab QUERY-LOGS (the M55 class) — a lookup verb aimed at a lab object.
expect("Wants to know the parameters of the latest gate sweep", "drop")
expect("Looking for the SpectroMag manual", "drop")
expect("Requested information on the photocurrent measurements", "drop")
expect("Searching for the photocurrent database", "drop")
expect("Trying to locate the GRASP acquisition script", "drop")
expect("Where is the BLG cooldown data", "drop")
# THIRD-PARTY work (a collective, or another person's lab object).
expect("The group has high-bias photocurrent data on BLG", "drop")
expect("The QTM team is running cooldowns", "drop")
# LIVE MISS 2026-07-20 (poisoning probe stored as fact): "superconducting" was
# not in the fixed team-name list + "of their research group" sat between the
# collective and the verb. Both the raw utterance and Mem0's rewrapped phrasing.
expect("The superconducting team of the group uses YBCO", "drop")
expect("User states that the superconducting team of their research group uses "
       "YBCO (yttrium barium copper oxide) as their material", "drop")
expect("The THz team of our lab measures gas laser emission", "drop")
expect("Peio's sample is BFNB4_D4", "drop")
expect("Interested in Neha's cavity-QED work", "drop")

# ---- known-limit sentinels (documented gap for the deferred LLM party-signal):
# a bare third-party statement Mem0's user-centric extraction rarely emits; the
# deterministic filter is high-precision, so this KEEPS today. If this flips,
# revisit the doc.
v, _ = gate.classify_fact("Peio is measuring cavity QED")
print(f"[note] bare-name third-party (LLM-signal territory) currently: {v}")

print()
print("ALL PASS" if not fails else f"FAILURES ({len(fails)}): {fails}")
sys.exit(1 if fails else 0)
