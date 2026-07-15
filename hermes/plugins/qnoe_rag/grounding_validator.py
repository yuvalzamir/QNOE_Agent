"""Deterministic post-hoc grounding validator (redteam R11).

Registered by qnoe_rag as a ``transform_llm_output`` hook: after the model
produces its final answer, extract every cited QCoDeS run id, ``.db`` path and
rooted file path, verify each against the registry + manifests, and append a
terse footer flagging any that do NOT exist. This is the hard backstop for the
R11 confabulation (invented run / fake ``/opt/qnoe-agent/qcodes_dbs/…`` path):
it fires even when the SOUL grounding rules slip, because it checks the OUTPUT.

Design (see plan / R11_GROUNDING_MITIGATION.md):
  * STRICT on run ids + ``.db`` paths — the registry is authoritative, so a
    non-existent one is a hard "unverified" flag.
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


def check(response_text: str) -> dict:
    """Pure analysis — returns the verdict without side effects (unit-testable).

    {'fab_runs': [...], 'fab_dbs': [...], 'unver_paths': [...],
     'n_runs': int, 'n_dbs': int, 'n_paths': int}
    """
    fab_runs: list[int] = []
    fab_dbs: list[str] = []
    unver_paths: list[str] = []

    seen_runs: set[int] = set()
    for m in _RUN_ID_RE.finditer(response_text):
        rid = int(m.group(1))
        if rid in seen_runs:
            continue
        seen_runs.add(rid)
        if not _run_exists(rid):
            fab_runs.append(rid)

    seen_paths: set[str] = set()
    dbs: list[str] = []
    for p in _ROOTED_PATH_RE.findall(response_text):
        if p in seen_paths:
            continue
        seen_paths.add(p)
        (dbs if p.lower().endswith(".db") else unver_paths).append(p)

    fab_dbs = [d for d in dbs if not _db_path_exists(d)]
    unver_paths = [p for p in unver_paths if not _file_path_exists(p)]

    return {
        "fab_runs": fab_runs,
        "fab_dbs": fab_dbs,
        "unver_paths": unver_paths,
        "n_runs": len(seen_runs),
        "n_dbs": len(dbs),
        "n_paths": len(seen_paths) - len(dbs),
    }


def _footer(fab_runs, fab_dbs, unver_paths) -> str:
    hard = [f"run {r}" for r in fab_runs] + list(fab_dbs)
    items = hard + (list(unver_paths) if (unver_paths and (hard or _flag_paths())) else [])
    if not items:
        return ""
    return (
        "\n\n> ⚠️ Unverified references: I could not confirm the following "
        "against the lab QCoDeS registry / file manifests, so treat them as "
        "unconfirmed and do not rely on them — " + "; ".join(items) + "."
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
            "grounding validate: runs=%d fab_runs=%s dbs=%d fab_dbs=%s "
            "paths=%d unver_paths=%s session=%s",
            v["n_runs"], v["fab_runs"], v["n_dbs"], v["fab_dbs"],
            v["n_paths"], v["unver_paths"], session_id,
        )
        footer = _footer(v["fab_runs"], v["fab_dbs"], v["unver_paths"])
        return (response_text + footer) if footer else None
    except Exception as exc:  # pragma: no cover — belt (hook layer also guards)
        logger.warning("grounding validate error (reply left unchanged): %s", exc)
        return None
