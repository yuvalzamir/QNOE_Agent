"""Deterministic first-party/third-party gate for Mem0 writes (MEMORY_ARCHITECTURE
Part 6 #2).

Mem0's extraction (its own prompt) surfaces candidate facts; this module is the
deterministic filter that decides which to KEEP and which to DROP, so a
third-party query-log or an objective lab record never lands in a user's
per-user memory (the M55 poisoning class). It operates on the *extracted fact
string* — which is much easier to classify than a raw multi-intent turn — and it
is the safety guarantee: an LLM party-label may be layered on later (route b in
the doc) but only ever to ADD drops, never to override a deterministic drop.

Calibration to REAL Mem0 phrasing (from the installed FACT_RETRIEVAL_PROMPT):
Mem0 emits terse, SUBJECT-LESS predicates about the user — "Name is John",
"Is a Software engineer", "Looking for a restaurant", "Prefers dark theme",
"Favourite movies are …", "Had a meeting with John". So we match verbs/objects,
not a "User asked" prefix. And because Mem0 is user-centric, "Sample is X"
already means the USER's sample (first-party) — we keep such focus pointers.

Policy (interim, until Cognee is the oracle):
  KEEP  — personal preferences, personal details, first-party FOCUS/interest
          POINTERS ("Prefers bullets", "Name is John", "Interested in BLG",
          "Sample is Tip5Sample9", "Is working on gate sweeps"), and generic
          non-lab facts (harmless personalization).
  DROP  — objective lab RECORDS (a run id, a .db, qcodes) — they belong in the
          registry/Cognee, not per-user memory, even for the user's own work;
          lab QUERY-LOGS (a lookup verb aimed at a lab object/doc/measurement);
          THIRD-PARTY work (a group/collective reference, or another person's
          possessive of a lab object).

Scope note: aggressive DROP rules fire only on LAB-related content — generic
personal facts ("Looking for a restaurant") are kept, they are not the
poisoning class. High-PRECISION by design (few false drops); recall of the
fuzzy context-dependent third-party cases (bare "Peio is measuring X", which
Mem0's user-centric extraction rarely emits anyway) is left to the deferred
LLM party-signal. Pure stdlib (`re`) — unit-tests standalone.
"""
from __future__ import annotations

import re

# Objective lab RECORD reference — a specific run id / .db / qcodes mention.
# These are registry/Cognee territory, never per-user memory (interim: not even
# for the user's own work). Run-id sub-pattern mirrors grounding_validator.
_RECORD_RE = re.compile(
    r"\brun[\s_]*(?:id|number|no\.?|#)?[\s_#:]*\d{1,7}\b"   # run 848, run #848, run id 848
    r"|\.db\b|\bqcodes\b",
    re.IGNORECASE,
)

# Query / lookup INTENT verbs — the M55 query-log class when aimed at a lab
# object. NOTE "interested in" is deliberately NOT here: a topic interest is a
# KEEP-worthy pointer, only an explicit lookup/ask is a query-log.
_QUERY_VERB_RE = re.compile(
    r"\b(asked?|asking|inquir\w+|requested?|requesting|"
    r"wants?\s+to\s+know|wanted\s+to\s+know|wondering|curious\s+about|"
    r"looking\s+for|searching\s+for|search(?:ed|ing)?\s+for|"
    r"trying\s+to\s+(?:find|locate)|where\s+(?:is|are)|\blocate\b|"
    r"question[s]?\s+about|queried|querying|wants?\s+info\w*)\b",
    re.IGNORECASE,
)

# Documents / files / a database a lookup might target.
_DOC_RE = re.compile(
    r"\b(file|files|document|documents|documentation|manual|manuals|notebook|"
    r"notebooks|script|scripts|paper|papers|report|reports|presentation|slides?|"
    r"spreadsheet|dataset|datasheet|guide|readme|database|"
    r"\.(?:py|ipynb|docx?|pdf|pptx?|xlsx?|md|csv|h5))\b",
    re.IGNORECASE,
)

# Measurement-type / lab-work vocabulary.
_MEAS_RE = re.compile(
    r"\b(gate[\s_-]*sweep|photocurrent|iv[\s_-]?(?:curve|sweep|measurement|meas)|"
    r"\biv\b|cooldown|sweep|measurement|spectroscopy|tunneling|transport|"
    r"band[\s_-]?structure)\b",
    re.IGNORECASE,
)

# General lab-domain vocabulary (samples, setups, materials, instruments).
_LAB_VOCAB_RE = re.compile(
    r"\b(sample|device|setup|cryostat|opticool|spectromag|grasp|"
    r"graphene|hbn|blg|slg|moo3|bscco|kagome|"
    r"l110|l206|l208|qtm)\b",
    re.IGNORECASE,
)

# THIRD-PARTY: a collective as the SUBJECT of work ("the group HAS data", "the
# QTM team IS running cooldowns"). Deliberately requires a work verb after the
# collective, so first-party MEMBERSHIP ("is a member of the QTM subteam") is
# NOT dropped.
_GROUP_SUBJECT_RE = re.compile(
    # Collective subject: "the group/lab", "another team", or "the <X> team" for
    # ANY single-word X ("the QTM team", "the superconducting team") — a fixed
    # sub-team-name list missed live 2026-07-20 ("superconducting" was said, the
    # list had "superconductivity"). An optional "of the/their (research) group"
    # tail is allowed before the verb ("the superconducting team of their
    # research group uses YBCO" — also missed live). The work verb is still
    # required, so first-party MEMBERSHIP ("is a member of the QTM subteam",
    # no verb after the collective) is NOT dropped.
    r"\b(the\s+group|the\s+lab|another\s+team|other\s+(?:team|group|people)|"
    r"the\s+[\w-]+\s+(?:team|group|sub-?team))"
    r"(?:\s+of\s+(?:the|their|our)\s+(?:research\s+)?(?:group|lab))?"
    r"\s+(?:members?\s+)?"
    r"(has|have|had|is|are|was|were|ran|runs|running|report\w*|measur\w*|owns?|"
    r"did|does|doing|stud\w+|collect\w*|working|use[sd]?|acquir\w+)\b",
    re.IGNORECASE,
)
# THIRD-PARTY: a collective POSSESSIVE ("the group's data", "the team's runs")
# or an explicit colleague reference.
_GROUP_POSSESS_RE = re.compile(
    r"\b(?:group|lab|team)['’]s\b|\bsomeone\s+else['’]s\b|\bcolleagues?\b",
    re.IGNORECASE,
)

# THIRD-PARTY: another person's POSSESSIVE of a lab object ("Peio's sample",
# "Neha's cavity-QED work", "Sergi's cooldown data"). Case-sensitive on the
# proper name so it does not fire on a sentence-initial common word ("Name is
# …"). The gap allows a few hyphenated tokens between the name and the object.
_POSSESSIVE_OTHER_RE = re.compile(
    r"\b[A-Z][a-z]+['’]s\s+(?:[\w-]+\s+){0,3}"
    r"(sample|samples|run|runs|db|database|measurement|measurements|project|"
    r"data|device|experiment|experiments|result|results|setup|cooldown|work|"
    r"analysis|paper|papers)\b",
)


def classify_fact(fact: str) -> tuple:
    """Return (verdict, reason): verdict is 'keep' or 'drop'."""
    if not fact or not fact.strip():
        return ("drop", "empty")
    f = fact.lower()
    # 1) objective lab record -> registry/Cognee, never per-user memory
    if _RECORD_RE.search(f):
        return ("drop", "lab-record")
    # 2) third-party: a collective doing work / possessive, or another person's
    #    lab object (membership like "member of the QTM subteam" is NOT dropped)
    if (_GROUP_SUBJECT_RE.search(f) or _GROUP_POSSESS_RE.search(f)
            or _POSSESSIVE_OTHER_RE.search(fact)):
        return ("drop", "third-party")
    # 3) lab query-log: a lookup verb aimed at a lab object / doc / measurement
    if _QUERY_VERB_RE.search(f) and (
        _DOC_RE.search(f) or _MEAS_RE.search(f) or _LAB_VOCAB_RE.search(f)
    ):
        return ("drop", "lab-query-log")
    # else: personal preference / interest pointer / first-party focus / generic
    return ("keep", "personal-or-firstparty")


def should_drop(fact: str) -> bool:
    return classify_fact(fact)[0] == "drop"
