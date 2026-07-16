"""Deterministic post-hoc grounding validator (redteam R11).

Registered by qnoe_rag as a ``transform_llm_output`` hook: after the model
produces its final answer, extract every cited QCoDeS run id, ``.db`` path and
rooted file path, verify each against the registry + manifests, and append a
terse footer flagging any that do NOT exist. This is the hard backstop for the
R11 confabulation (invented run / fake ``/opt/qnoe-agent/qcodes_dbs/…`` path):
it fires even when the SOUL grounding rules slip, because it checks the OUTPUT.

Also catches MISATTRIBUTION of REAL entities (R11 #2, see
R11_MISATTRIBUTION_PLAN.md): a reply may cite a real run against the WRONG db
(run↔DB — run_id is per-database), or a run that IS in the cited db but is
mislabelled as the wrong measurement type (run↔type — e.g. an IV run called a
"gate-sweep"). Runs are paired to their cited db from the reply text (same-line
/ sticky "same DB"), then verified against the (db_path, run_id) composite and
the claimed-type header.

Design (see plan / R11_GROUNDING_MITIGATION.md):
  * STRICT on run ids + ``.db`` paths + run↔DB mismatch — the registry is
    authoritative, so a non-existent / wrong-db one is a hard flag.
  * ADVISORY on run↔type (heuristic; toggle QNOE_GROUNDING_CHECK_TYPE).
  * ADVISORY + DEFAULT-OFF on run↔sample / run↔params
    (QNOE_GROUNDING_CHECK_SAMPLE_PARAMS): the registry sample_name is verbose
    free text that diverges from the folder name users cite, and params are
    channel tokens not physics notation, so a claim is accepted from EITHER
    source (sample_name field OR db path; channel-token match only) and only a
    citation matching NEITHER is flagged. Off until live-tuned.
  * ADVISORY on other file paths — they may be real-but-unindexed (e.g. a
    permission-locked project folder), so they are logged and only footered
    when QNOE_GROUNDING_FLAG_PATHS is enabled.
  * FAIL-OPEN — any DB/read error treats the ref as existing (never cry wolf on
    a transient hiccup); any exception leaves the reply unchanged.
  * FLAG, don't strip/regenerate — append a footer, never edit the body.

Kept intentionally self-contained (own regexes + DB paths, no import back into
qnoe_rag) so it unit-tests standalone. Keep _RUN_ID_RE / DB paths in sync with
qnoe_rag/__init__.py.
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3

# Log under qnoe_rag's captured namespace — a bare module-name logger (this
# module is loaded via importlib) does not propagate to the gateway's captured
# handlers, so the per-turn verdict line would be invisible.
logger = logging.getLogger("_hermes_user_memory.qnoe_rag.grounding")


def _enabled() -> bool:
    return os.environ.get("QNOE_GROUNDING_VALIDATE", "1").lower() not in (
        "0", "false", "off", "no",
    )


def _flag_paths() -> bool:
    # Advisory file-path misses only footer when explicitly enabled (off by
    # default to avoid false alarms on real-but-unindexed files).
    return os.environ.get("QNOE_GROUNDING_FLAG_PATHS", "0").lower() in (
        "1", "true", "on", "yes",
    )


# episodic.db holds BOTH qcodes_registry AND index_manifest (server + repo).
REGISTRY_DBS = [
    os.path.join(
        os.environ.get("AGENT_DATA_DIR", "/home/yzamir/qnoe_server_data"),
        "episodic.db",
    ),
    "/opt/qnoe-agent/memory/episodic.db",
]
SP_MANIFEST_DB = os.environ.get(
    "SP_MANIFEST_DB", "/opt/qnoe-agent/memory/sharepoint.db"
)

# "run 848", "run_848", "run #848", "run id 848", "run number 848" (mirror of
# qnoe_rag._RUN_ID_RE).
_RUN_ID_RE = re.compile(
    r"\brun[\s_]*(?:with[\s_]+)?(?:id|number|no\.?|#)?[\s_#:]*(\d{1,7})\b",
    re.IGNORECASE,
)
# A rooted absolute path (…/foo.db or …/foo.ext). Stops at whitespace, quotes,
# parens, backticks, commas so it doesn't swallow trailing prose.
_ROOTED_PATH_RE = re.compile(r"/[^\s\"'`(),]+?\.[A-Za-z0-9]{1,5}\b")


def _connect_ro(db: str, immutable: bool = False) -> sqlite3.Connection:
    uri = f"file:{db}?mode=ro" + ("&immutable=1" if immutable else "")
    return sqlite3.connect(uri, uri=True, timeout=3)


def _run_exists(run_id: int) -> bool:
    for db in REGISTRY_DBS:
        if not os.path.exists(db):
            continue
        try:
            con = _connect_ro(db)
            try:
                if con.execute(
                    "SELECT 1 FROM qcodes_registry WHERE run_id=? LIMIT 1",
                    (run_id,),
                ).fetchone():
                    return True
            finally:
                con.close()
        except sqlite3.Error:
            return True  # fail-open: never flag on a DB error
    return False


def _esc(s: str) -> str:
    """Escape LIKE wildcards so a path's own '_' / '%' aren't treated as
    wildcards (paths are full of underscores)."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _suffix_exists(db: str, table: str, cols: tuple, ref: str,
                   immutable: bool = False) -> bool:
    """True if any row has a column ENDING with `ref` (suffix LIKE).

    Suffix match is the right tool: the path regex truncates space-containing
    paths (common — 'L110 QTM', 'IV - meas') to their space-free tail, so an
    exact match false-flags real paths; a bare-basename match ('DB.db') is too
    generic and lets fabricated full paths pass. The captured tail (parent
    dir(s) + basename) is distinctive enough to catch fabrications while a real
    (truncated) path still matches. Fail-open on error."""
    ref = ref.strip()
    if not ref:
        return True
    like = f"%{_esc(ref)}"
    where = " OR ".join(f"{c} LIKE ? ESCAPE '\\'" for c in cols)
    try:
        con = _connect_ro(db, immutable=immutable)
        try:
            return con.execute(
                f"SELECT 1 FROM {table} WHERE {where} LIMIT 1",
                tuple(like for _ in cols),
            ).fetchone() is not None
        finally:
            con.close()
    except sqlite3.Error:
        return True  # fail-open


def _db_path_exists(dbpath: str) -> bool:
    return any(
        os.path.exists(db) and _suffix_exists(db, "qcodes_registry", ("db_path",), dbpath)
        for db in REGISTRY_DBS
    )


def _file_path_exists(path: str) -> bool:
    for db in REGISTRY_DBS:
        if os.path.exists(db) and _suffix_exists(db, "index_manifest", ("file_path",), path):
            return True
    if os.path.exists(SP_MANIFEST_DB) and _suffix_exists(
        SP_MANIFEST_DB, "sp_manifest", ("item_path", "web_url"), path, immutable=True
    ):
        return True
    return False


# Negation cues near a reference → the model is DENYING it exists (e.g. "no run
# 999999 exists", "could not find …"), not asserting it as real. Don't
# double-flag those — the footer is for fabrications ASSERTED as real.
_DENIAL_RE = re.compile(
    r"no run|does\s*n['o]?t?\s*exist|doesn't exist|not\s+exist|no such|no entry|"
    r"not\s+found|could\s*n['o]?t?\s*find|cannot find|can't find|not in the "
    r"registry|no matching|no record|unverified|unconfirmed|no data for",
    re.IGNORECASE,
)
_DENIAL_WINDOW = 90  # chars each side of the reference


def _denied(text: str, start: int, end: int) -> bool:
    ctx = text[max(0, start - _DENIAL_WINDOW): end + _DENIAL_WINDOW]
    return _DENIAL_RE.search(ctx) is not None


# --------------------------------------------------------------------------- #
# MISATTRIBUTION (redteam R11 #2): a reply may cite REAL run ids against the
# WRONG db (run↔DB), or a run that IS in the cited db but is mislabelled as the
# wrong measurement type (run↔type). run_id is per-database (composite key
# (db_path, run_id)), so existence-only checks miss both. See
# R11_MISATTRIBUTION_PLAN.md.


def _check_type() -> bool:
    # The run↔type check is heuristic (advisory) — toggle off if noisy.
    return os.environ.get("QNOE_GROUNDING_CHECK_TYPE", "1").lower() not in (
        "0", "false", "off", "no",
    )


# "same DB", "same database", "same file" → a run row that reuses the db from a
# nearby preceding line (list format: db printed once, later rows say "same DB").
_SAME_DB_RE = re.compile(r"\bsame\s+(?:db|database|file)\b", re.IGNORECASE)

# Measurement-type rules: (CLAIMED detector in the REPLY, VERIFIER of the
# registry run_name+exp_name, label). The verifier looks for the measurement
# TYPE PHRASE (e.g. 'gate_sweep', 'sweep gate') — NOT a bare word — so a device
# name like 'Gated_Graphene' in exp_name does NOT read as a gate SWEEP, and the
# real run_name 'gate_sweep_Vg…' still verifies. Type lives in run_name OR
# exp_name free text (canonical gate sweeps sometimes carry it in exp_name).
_TYPE_RULES = [
    (re.compile(r"gate[\s_-]*sweep|back[\s_-]*gate[\s_-]*sweep|\bvg\b[\s_-]*sweep", re.IGNORECASE),
     re.compile(r"gate[\s_-]*sweep|sweep[\s_-]*gate|\bvg[\s_-]*sweep|sweep[\s_-]*vg", re.IGNORECASE),
     "gate-sweep"),
    (re.compile(r"\bi[\s]?[-–/]?\s?v\b[\s_-]*(?:sweep|measurement|meas|curve|runs?)|bias[\s_-]*sweep|current[\s_-]*voltage", re.IGNORECASE),
     re.compile(r"\biv[\s_-]|[\s_-]iv\b|\bi[\s_-]?v[\s_-]|bias[\s_-]*sweep", re.IGNORECASE),
     "IV"),
    (re.compile(r"photocurrent", re.IGNORECASE),
     re.compile(r"photo[\s_-]*current", re.IGNORECASE),
     "photocurrent"),
    (re.compile(r"temp(?:erature)?[\s_-]*(?:sweep|depend)|cooldown", re.IGNORECASE),
     re.compile(r"temp[\s_-]*depend|temperature[\s_-]*sweep|cooldown|\bt[\s_-]*sweep|vs[\s_-]*t\b", re.IGNORECASE),
     "temperature"),
]
_CLAIMED_TYPE_LOOKBACK = 300  # chars before a run to find its type header


def _short(db: str) -> str:
    parts = [p for p in db.split("/") if p]
    return ".../" + "/".join(parts[-2:]) if len(parts) > 2 else db


def _distinctive(db_ref: str) -> bool:
    # Need ≥2 path segments (parent + basename) — a bare 'DB.db' matches many
    # real dbs, so treat it as non-distinctive (fail-open, never flag).
    return len([p for p in db_ref.split("/") if p]) >= 2


def _run_in_db(run_id: int, db_ref: str) -> tuple:
    """Return (db_exists, run_in_db) for the (db, run) composite. Fail-open
    (True, True) on a non-distinctive ref or any DB error, so we never flag on
    ambiguity."""
    if not _distinctive(db_ref):
        return (True, True)
    like = f"%{_esc(db_ref)}"
    db_exists = run_in = False
    for db in REGISTRY_DBS:
        if not os.path.exists(db):
            continue
        try:
            con = _connect_ro(db)
            try:
                if con.execute(
                    "SELECT 1 FROM qcodes_registry WHERE db_path LIKE ? ESCAPE '\\' LIMIT 1",
                    (like,),
                ).fetchone():
                    db_exists = True
                if con.execute(
                    "SELECT 1 FROM qcodes_registry WHERE run_id=? AND db_path LIKE ? ESCAPE '\\' LIMIT 1",
                    (run_id, like),
                ).fetchone():
                    run_in = True
            finally:
                con.close()
        except sqlite3.Error:
            return (True, True)  # fail-open
    return (db_exists, run_in)


def _row_type_text(run_id: int, db_ref: str) -> "str | None":
    """lower(run_name + ' ' + exp_name) of the (db, run) row, or None."""
    if not _distinctive(db_ref):
        return None
    like = f"%{_esc(db_ref)}"
    for db in REGISTRY_DBS:
        if not os.path.exists(db):
            continue
        try:
            con = _connect_ro(db)
            try:
                r = con.execute(
                    "SELECT run_name, exp_name FROM qcodes_registry "
                    "WHERE run_id=? AND db_path LIKE ? ESCAPE '\\' LIMIT 1",
                    (run_id, like),
                ).fetchone()
                if r:
                    return f"{r[0] or ''} {r[1] or ''}".lower()
            finally:
                con.close()
        except sqlite3.Error:
            return None
    return None


def _claimed_type(text: str, run_start: int) -> "tuple | None":
    """A measurement-type header/label just before (or on) the run's line →
    (verifier_regex, label) for the claimed type, else None."""
    window = text[max(0, run_start - _CLAIMED_TYPE_LOOKBACK): run_start + 40]
    for claimed_re, verifier_re, label in _TYPE_RULES:
        if claimed_re.search(window):
            return (verifier_re, label)
    return None


def _pair_runs_to_dbs(text: str) -> list:
    """Return [(run_id, start, end, cited_db_or_None)] for every run mention.

    A run pairs to a .db path on the SAME LINE; a run on a line that says
    'same db' pairs (sticky) to the most-recent db from a nearby preceding line
    (list format prints the db once). Otherwise cited_db is None (falls back to
    the weaker existence-only check)."""
    out = []
    last_db = None
    last_db_lineno = -99
    offset = 0
    for lineno, line in enumerate(text.splitlines(keepends=True)):
        db_on_line = None
        for dm in _ROOTED_PATH_RE.finditer(line):
            if dm.group(0).lower().endswith(".db"):
                db_on_line = dm.group(0)
                break
        if db_on_line:
            last_db, last_db_lineno = db_on_line, lineno
        sticky = (
            last_db
            if db_on_line is None and _SAME_DB_RE.search(line)
            and last_db and (lineno - last_db_lineno) <= 6
            else None
        )
        line_db = db_on_line or sticky
        for rm in _RUN_ID_RE.finditer(line):
            out.append((int(rm.group(1)), offset + rm.start(), offset + rm.end(), line_db))
        offset += len(line)
    return out


# --------------------------------------------------------------------------- #
# run↔SAMPLE / run↔PARAMS (redteam R11 #2 follow-up). A reply may cite a real
# run (correctly in its db) but attach the WRONG sample name or a parameter the
# run never recorded. HIGH false-positive surface — the registry sample_name is
# verbose free text ("Sample: Grated_Graphene_3L_hBN_Sample9 and Tip: …") that
# diverges from the folder name users say ("Tip5Sample9"), and params are stored
# as channel tokens (["gate","dc_current"]) not physics notation ("gate
# voltage", "Vbg"). So both checks are ADVISORY, default-OFF, and accept a claim
# from EITHER authoritative source; they only flag a citation that matches
# NEITHER. See R11_MISATTRIBUTION_PLAN.md "run↔sample/params".


def _check_sample_params() -> bool:
    # OFF by default — notation divergence (folder-vs-field sample names,
    # channel-vs-physics param names) means this needs live tuning before it can
    # fire in production without crying wolf. Enable to evaluate / after tuning.
    return os.environ.get("QNOE_GROUNDING_CHECK_SAMPLE_PARAMS", "0").lower() in (
        "1", "true", "on", "yes",
    )


# A sample/device/chip name asserted next to a run. Only DISTINCTIVE tokens (a
# digit, an underscore, or CamelCase) are trusted as a real claim — this skips
# prose like "sample the device was…" that would otherwise capture "the".
_SAMPLE_CLAIM_RE = re.compile(
    r"\b(?:sample|device|chip)\b\s*(?:name)?\s*(?:[:=]|is|was|named)?\s*"
    r"[\"“'‘]?([A-Za-z0-9][\w.\-]{2,40})",
    re.IGNORECASE,
)
# Parameter-context cue → the following short segment names parameters.
_PARAM_CUE_RE = re.compile(
    r"\b(param(?:eter)?s?|swept|sweeping|measured|measures|measuring|"
    r"recorded|records|channels?|acquired)\b",
    re.IGNORECASE,
)
# Registry-style channel token (lowercase_underscore, ≥2 segments). Physics
# notation (Vbg, Rxx, "gate voltage") deliberately does NOT match → skipped.
_PARAM_TOKEN_RE = re.compile(r"\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b")
_SP_BACK = 120   # chars before the run to scan for an attached sample/param claim
_SP_FWD = 220    # chars after


def _row_sample_params(run_id: int, db_ref: str) -> "tuple | None":
    """(sample_name_lower, db_path_lower, parameters_lower) for the (db, run)
    row, or None (non-distinctive ref / not found / DB error → fail-open)."""
    if not _distinctive(db_ref):
        return None
    like = f"%{_esc(db_ref)}"
    for db in REGISTRY_DBS:
        if not os.path.exists(db):
            continue
        try:
            con = _connect_ro(db)
            try:
                r = con.execute(
                    "SELECT sample_name, db_path, parameters FROM qcodes_registry "
                    "WHERE run_id=? AND db_path LIKE ? ESCAPE '\\' LIMIT 1",
                    (run_id, like),
                ).fetchone()
                if r:
                    return ((r[0] or "").lower(), (r[1] or "").lower(), (r[2] or "").lower())
            finally:
                con.close()
        except sqlite3.Error:
            return None
    return None


def _claimed_samples(text: str, s: int, e: int) -> list:
    window = text[max(0, s - _SP_BACK): e + _SP_FWD]
    out = []
    for m in _SAMPLE_CLAIM_RE.finditer(window):
        tok = m.group(1).rstrip(".,:;)\"'’”")   # trailing prose punctuation
        if (re.search(r"\d|_", tok) or re.search(r"[a-z][A-Z]", tok)) and tok not in out:
            out.append(tok)
    return out


def _claimed_params(text: str, s: int, e: int) -> list:
    window = text[max(0, s - 20): e + _SP_FWD]
    out = []
    for cue in _PARAM_CUE_RE.finditer(window):
        seg = re.split(r"[.;\n]", window[cue.end(): cue.end() + 80])[0]
        for t in _PARAM_TOKEN_RE.findall(seg):
            if t not in out:
                out.append(t)
    return out


def check(response_text: str) -> dict:
    """Pure analysis — returns the verdict without side effects (unit-testable).

    Returns fab_runs / fab_dbs / unver_paths (nonexistent, R11 v1) PLUS
    misattributed_runs [(run, cited_db)] (real run, wrong db) and mistyped_runs
    [(run, cited_db, claimed, actual)] (real run in the right db, wrong type).
    References the model itself flags as nonexistent (denial context) are NOT
    reported — only references asserted as real but wrong.
    """
    fab_runs: list[int] = []
    misattributed: list[tuple] = []
    mistyped: list[tuple] = []
    missample: list[tuple] = []
    misparam: list[tuple] = []
    check_type = _check_type()
    check_sp = _check_sample_params()

    seen_runs: set[int] = set()
    for run_id, s, e, cited_db in _pair_runs_to_dbs(response_text):
        if run_id in seen_runs:
            continue
        seen_runs.add(run_id)
        if _denied(response_text, s, e):
            continue
        if not _run_exists(run_id):
            fab_runs.append(run_id)                      # nonexistent anywhere
            continue
        if cited_db is None:
            continue                                     # unpaired real run — nothing to check
        db_exists, run_in = _run_in_db(run_id, cited_db)
        if db_exists and not run_in:
            misattributed.append((run_id, cited_db))     # real run, wrong db
            continue
        if check_type and run_in:
            claimed = _claimed_type(response_text, s)
            if claimed:
                verifier_re, label = claimed
                tt = _row_type_text(run_id, cited_db)
                if tt is not None and not verifier_re.search(tt):
                    mistyped.append((run_id, cited_db, label, (tt.strip() or "<unnamed>")[:50]))
        if check_sp and run_in:
            rsp = _row_sample_params(run_id, cited_db)
            if rsp is not None:
                sample_l, dbpath_l, params_l = rsp
                for tok in _claimed_samples(response_text, s, e):
                    tl = tok.lower()
                    if tl not in sample_l and tl not in dbpath_l:  # neither field nor path
                        missample.append(
                            (run_id, cited_db, tok, (sample_l.strip() or "<none>")[:50]))
                for tok in _claimed_params(response_text, s, e):
                    if tok not in params_l:  # channel token the run never recorded
                        misparam.append((run_id, cited_db, tok))

    seen_paths: set[str] = set()
    dbs: list[tuple] = []          # (path, start, end)
    paths: list[tuple] = []
    for m in _ROOTED_PATH_RE.finditer(response_text):
        p = m.group(0)
        if p in seen_paths:
            continue
        seen_paths.add(p)
        (dbs if p.lower().endswith(".db") else paths).append((p, m.start(), m.end()))

    fab_dbs = [p for (p, s, e) in dbs
               if not _db_path_exists(p) and not _denied(response_text, s, e)]
    unver_paths = [p for (p, s, e) in paths
                   if not _file_path_exists(p) and not _denied(response_text, s, e)]

    return {
        "fab_runs": fab_runs,
        "fab_dbs": fab_dbs,
        "unver_paths": unver_paths,
        "misattributed_runs": misattributed,
        "mistyped_runs": mistyped,
        "missample_runs": missample,
        "misparam_runs": misparam,
        "n_runs": len(seen_runs),
        "n_dbs": len(dbs),
        "n_paths": len(paths),
    }


def _footer(v: dict) -> str:
    items: list[str] = []
    items += [f"run {r} (no such run)" for r in v["fab_runs"]]
    items += [f"{_short(d)} (no such database)" for d in v["fab_dbs"]]
    items += [f"run {r} is not in the database cited ({_short(db)})"
              for (r, db) in v["misattributed_runs"]]
    items += [f"run {r} in {_short(db)} is not a {claimed} run (registry: “{actual}”)"
              for (r, db, claimed, actual) in v["mistyped_runs"]]
    items += [f"run {r} in {_short(db)} is not associated with sample “{tok}” "
              f"(registry sample: “{actual}”)"
              for (r, db, tok, actual) in v["missample_runs"]]
    items += [f"parameter “{tok}” is not recorded for run {r} in {_short(db)}"
              for (r, db, tok) in v["misparam_runs"]]
    hard = bool(items)
    if v["unver_paths"] and (hard or _flag_paths()):
        items += [f"{_short(p)} (not found)" for p in v["unver_paths"]]
    if not items:
        return ""
    return (
        "\n\n> ⚠️ Unverified references — I could not confirm the following "
        "against the lab QCoDeS registry / file manifests, so treat them as "
        "unconfirmed and do not rely on them: " + "; ".join(items) + "."
    )


def validate_reply(*, response_text: str = None, session_id: str = "",
                   model: str = "", platform: str = "", **_) -> "str | None":
    """transform_llm_output hook. Returns augmented text, or None to leave the
    reply unchanged. Fail-open on any error."""
    if not _enabled() or not response_text:
        return None
    try:
        v = check(response_text)
        logger.info(
            "grounding validate: runs=%d fab_runs=%s misattr=%s mistyped=%s "
            "missample=%s misparam=%s dbs=%d fab_dbs=%s paths=%d unver_paths=%s "
            "session=%s",
            v["n_runs"], v["fab_runs"], v["misattributed_runs"], v["mistyped_runs"],
            v["missample_runs"], v["misparam_runs"], v["n_dbs"], v["fab_dbs"],
            v["n_paths"], v["unver_paths"], session_id,
        )
        footer = _footer(v)
        return (response_text + footer) if footer else None
    except Exception as exc:  # pragma: no cover — belt (hook layer also guards)
        logger.warning("grounding validate error (reply left unchanged): %s", exc)
        return None
