"""QNOE QCoDeS measurement registry plugin for Hermes Agent.

Exposes three tools that query the ``qcodes_registry`` SQLite table
(populated by the QCoDeS scanner during ingestion/nightly indexing).
The registry DB lives at ``$AGENT_DATA_DIR/episodic.db``.

Tools
-----
qcodes_search
    Find runs by sample name, experiment type, date range, or free text.
    Returns run cards with db_path + run_id identifiers for the other tools.

qcodes_run_details
    Fetch full parameter details for one run: swept axes and measured
    parameters, each with label and unit, parsed from the stored
    run_description JSON.

qcodes_run_diff
    Compare two runs side-by-side. Returns which swept axes and measured
    parameters differ between them.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

AGENT_DATA_DIR = os.environ.get("AGENT_DATA_DIR", "/home/yzamir/qnoe_server_data")
REGISTRY_DB = os.path.join(AGENT_DATA_DIR, "episodic.db")
# Lab-server registry + SharePoint-ingest registry (identical schema).
# Both must be searched — see memory/mistakes.md M44. Colon-separated override.
REGISTRY_DBS = [
    p
    for p in os.environ.get(
        "QCODES_REGISTRY_DBS",
        REGISTRY_DB + ":/opt/qnoe-agent/memory/episodic.db",
    ).split(":")
    if p
]

MAX_RESULTS = 50

# ---------------------------------------------------------------------------
# Schemas  (kept thin — descriptions drive LLM behaviour, not token count)
# ---------------------------------------------------------------------------

QCODES_SEARCH_SCHEMA = {
    "name": "qcodes_search",
    "description": (
        "Search the QCoDeS measurement registry for experiment runs. "
        "Returns run cards with experiment name, sample, run name, swept + "
        "measured parameters, timestamp, and source database path. Use to "
        "find measurements by sample, experiment, setup path, or date range. "
        "IMPORTANT: for 'X sweep' questions (gate sweep, field sweep, bias "
        "sweep) use the swept_parameter filter — it matches what was "
        "actually swept, which free-text matching does not."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Free-text search across experiment name, sample name, "
                    "run name, and parameters. Matched with SQL LIKE."
                ),
            },
            "sample": {
                "type": "string",
                "description": "Filter by sample name (partial match).",
            },
            "experiment": {
                "type": "string",
                "description": "Filter by experiment name (partial match).",
            },
            "date_from": {
                "type": "string",
                "description": "Start date filter (ISO format, e.g. 2026-01-01).",
            },
            "date_to": {
                "type": "string",
                "description": "End date filter (ISO format, e.g. 2026-06-30).",
            },
            "swept_parameter": {
                "type": "string",
                "description": (
                    "Filter by the SWEPT (independent) parameter name/label, "
                    "partial match — e.g. 'gate' finds true gate sweeps. Use "
                    "this for questions like 'last gate sweep' or 'field "
                    "sweeps'; free-text query only matches names, not what "
                    "was actually swept."
                ),
            },
            "path": {
                "type": "string",
                "description": (
                    "Filter by database file path (partial match), e.g. "
                    "'L110 QTM' for the QTM room-T setup."
                ),
            },
            "limit": {
                "type": "integer",
                "description": f"Max results (default 20, max {MAX_RESULTS}).",
            },
        },
        "required": [],
    },
}

QCODES_RUN_DETAILS_SCHEMA = {
    "name": "qcodes_run_details",
    "description": (
        "Get full parameter details for a QCoDeS run: swept axes and "
        "measured parameters with labels and units. "
        "Supply db_path and run_id from qcodes_search results."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "db_path": {"type": "string", "description": "DB file path from qcodes_search."},
            "run_id": {"type": "integer", "description": "Run ID from qcodes_search."},
        },
        "required": ["db_path", "run_id"],
    },
}

QCODES_RUN_DIFF_SCHEMA = {
    "name": "qcodes_run_diff",
    "description": (
        "Compare two QCoDeS runs: shows which swept axes and measured "
        "parameters differ between them. "
        "Supply db_path and run_id for each run from qcodes_search results."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "db_path_a": {"type": "string", "description": "DB path for run A."},
            "run_id_a": {"type": "integer", "description": "Run ID for run A."},
            "db_path_b": {"type": "string", "description": "DB path for run B."},
            "run_id_b": {"type": "integer", "description": "Run ID for run B."},
        },
        "required": ["db_path_a", "run_id_a", "db_path_b", "run_id_b"],
    },
}

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(REGISTRY_DB)
    conn.row_factory = sqlite3.Row
    return conn


def _iso_to_epoch(iso_str: str) -> float:
    """Convert ISO date string to Unix epoch for timestamp comparison."""
    try:
        if "T" in iso_str:
            dt = datetime.fromisoformat(iso_str)
        else:
            dt = datetime.strptime(iso_str, "%Y-%m-%d")
        return dt.replace(tzinfo=timezone.utc).timestamp()
    except (ValueError, TypeError):
        return 0.0


def _epoch_to_iso(epoch) -> str:
    """Convert Unix epoch (float or string) to human-readable ISO datetime."""
    try:
        val = float(epoch) if epoch else 0.0
        if val > 0:
            return datetime.fromtimestamp(val, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        return ""
    except (ValueError, TypeError, OSError):
        return str(epoch)


def _connect_ro(db: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
    conn.row_factory = sqlite3.Row
    return conn


def _fetch_run(db_path: str, run_id: int) -> Optional[Dict[str, Any]]:
    """Fetch a single run record by (db_path, run_id) from any registry."""
    for db in REGISTRY_DBS:
        if not os.path.exists(db):
            continue
        try:
            conn = _connect_ro(db)
            try:
                row = conn.execute(
                    "SELECT * FROM qcodes_registry WHERE db_path=? AND run_id=?",
                    (db_path, run_id),
                ).fetchone()
                if row:
                    return dict(row)
            finally:
                conn.close()
        except Exception as exc:
            logger.warning("Registry read failed for %s: %s", db, exc)
    return None


# ---------------------------------------------------------------------------
# Parameter parsing
# ---------------------------------------------------------------------------


def _parse_params(description_json: Optional[str]) -> Dict[str, Any]:
    """Parse a QCoDeS run_description JSON blob into swept/measured lists."""
    if not description_json:
        return {"swept": [], "measured": []}
    try:
        desc = json.loads(description_json)
        inter = desc.get("interdependencies_", {})
        all_params = inter.get("parameters", {})
        dependencies = inter.get("dependencies", {})

        measured_names = set(dependencies.keys())
        swept_names = {p for deps in dependencies.values() for p in deps}

        def _info(name: str) -> Dict[str, str]:
            p = all_params.get(name, {})
            return {"name": name, "label": p.get("label", name), "unit": p.get("unit", "")}

        return {
            "swept": [_info(n) for n in sorted(swept_names)],
            "measured": [_info(n) for n in sorted(measured_names)],
        }
    except Exception:
        logger.debug("Failed to parse description_json", exc_info=True)
        return {"swept": [], "measured": [], "parse_error": True}


def _diff_param_lists(
    list_a: List[Dict], list_b: List[Dict]
) -> Dict[str, Any]:
    """Diff two parameter lists by name."""
    map_a = {p["name"]: p for p in list_a}
    map_b = {p["name"]: p for p in list_b}
    return {
        "only_in_a": [p for n, p in map_a.items() if n not in map_b],
        "only_in_b": [p for n, p in map_b.items() if n not in map_a],
        "in_both": sorted(n for n in map_a if n in map_b),
    }


# ---------------------------------------------------------------------------
# qcodes_search
# ---------------------------------------------------------------------------


def _search(
    query: str = "",
    sample: str = "",
    experiment: str = "",
    date_from: str = "",
    date_to: str = "",
    swept_parameter: str = "",
    path: str = "",
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """Query all registries with optional filters.

    ``swept_parameter`` is verified against the parsed run description
    (the truly swept axes), not just text matching — the LIKE on
    description_json below is only a cheap prefilter.
    """
    conditions: List[str] = []
    params: list = []

    if query:
        conditions.append(
            "(exp_name LIKE ? OR sample_name LIKE ? OR run_name LIKE ? OR parameters LIKE ?)"
        )
        q = f"%{query}%"
        params.extend([q, q, q, q])
    if sample:
        conditions.append("sample_name LIKE ?")
        params.append(f"%{sample}%")
    if experiment:
        conditions.append("exp_name LIKE ?")
        params.append(f"%{experiment}%")
    if path:
        conditions.append("db_path LIKE ?")
        params.append(f"%{path}%")
    if swept_parameter:
        conditions.append("description_json LIKE ?")
        params.append(f"%{swept_parameter}%")
    if date_from:
        conditions.append("completed_timestamp >= ?")
        params.append(_iso_to_epoch(date_from))
    if date_to:
        conditions.append("completed_timestamp <= ?")
        params.append(_iso_to_epoch(date_to + "T23:59:59"))

    where = " AND ".join(conditions) if conditions else "1=1"
    limit = min(max(1, limit), MAX_RESULTS)
    # Over-fetch when the swept filter will prune rows post-parse.
    fetch_n = limit * 10 if swept_parameter else limit

    sql = f"""
        SELECT db_path, run_id, exp_name, sample_name, run_name,
               parameters, completed_timestamp, description_json
        FROM qcodes_registry
        WHERE {where}
        ORDER BY completed_timestamp DESC
        LIMIT ?
    """
    rows: List[Dict[str, Any]] = []
    for db in REGISTRY_DBS:
        if not os.path.exists(db):
            continue
        try:
            conn = _connect_ro(db)
            try:
                rows.extend(dict(r) for r in conn.execute(sql, params + [fetch_n]).fetchall())
            finally:
                conn.close()
        except Exception as exc:
            logger.warning("Registry search failed for %s: %s", db, exc)

    if swept_parameter:
        needle = swept_parameter.lower()
        rows = [
            r
            for r in rows
            if any(
                needle in s["name"].lower() or needle in (s.get("label") or "").lower()
                for s in _parse_params(r.get("description_json"))["swept"]
            )
        ]

    def _ts(r: Dict[str, Any]) -> float:
        try:
            return float(r.get("completed_timestamp") or 0)
        except (TypeError, ValueError):
            return 0.0

    rows.sort(key=_ts, reverse=True)
    return rows[:limit]


def _handle_qcodes_search(args: Dict[str, Any]) -> str:
    try:
        rows = _search(
            query=args.get("query", ""),
            sample=args.get("sample", ""),
            experiment=args.get("experiment", ""),
            date_from=args.get("date_from", ""),
            date_to=args.get("date_to", ""),
            swept_parameter=args.get("swept_parameter", ""),
            path=args.get("path", ""),
            limit=args.get("limit", 20),
        )
        if not rows:
            return json.dumps({"result": "No matching measurements found.", "count": 0})
        results = []
        for r in rows:
            p = _parse_params(r.get("description_json"))
            results.append({
                "db_path": r["db_path"],
                "run_id": r["run_id"],
                "experiment": r["exp_name"],
                "sample": r["sample_name"],
                "run_name": r["run_name"],
                "swept": [s["name"] for s in p["swept"]],
                "measured": [m["name"] for m in p["measured"]],
                "timestamp": _epoch_to_iso(r["completed_timestamp"]),
            })
        return json.dumps({"results": results, "count": len(results)})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ---------------------------------------------------------------------------
# qcodes_run_details
# ---------------------------------------------------------------------------


def _handle_qcodes_run_details(args: Dict[str, Any]) -> str:
    db_path = args.get("db_path", "")
    run_id = args.get("run_id")
    if not db_path or run_id is None:
        return json.dumps({"error": "db_path and run_id are required."})

    row = _fetch_run(db_path, int(run_id))
    if not row:
        return json.dumps({"error": f"Run {run_id} not found in registry for {db_path}."})

    params = _parse_params(row.get("description_json"))
    return json.dumps({
        "db_path": row["db_path"],
        "run_id": row["run_id"],
        "experiment": row["exp_name"],
        "sample": row["sample_name"],
        "run_name": row["run_name"],
        "timestamp": _epoch_to_iso(row["completed_timestamp"]),
        "swept": params["swept"],
        "measured": params["measured"],
    })


# ---------------------------------------------------------------------------
# qcodes_run_diff
# ---------------------------------------------------------------------------


def _handle_qcodes_run_diff(args: Dict[str, Any]) -> str:
    db_path_a = args.get("db_path_a", "")
    run_id_a = args.get("run_id_a")
    db_path_b = args.get("db_path_b", "")
    run_id_b = args.get("run_id_b")

    if not all([db_path_a, run_id_a is not None, db_path_b, run_id_b is not None]):
        return json.dumps({"error": "db_path_a, run_id_a, db_path_b, run_id_b are all required."})

    row_a = _fetch_run(db_path_a, int(run_id_a))
    row_b = _fetch_run(db_path_b, int(run_id_b))

    if not row_a:
        return json.dumps({"error": f"Run A not found: {db_path_a} #{run_id_a}."})
    if not row_b:
        return json.dumps({"error": f"Run B not found: {db_path_b} #{run_id_b}."})

    params_a = _parse_params(row_a.get("description_json"))
    params_b = _parse_params(row_b.get("description_json"))

    def _summary(row: Dict) -> Dict:
        return {
            "db_path": row["db_path"],
            "run_id": row["run_id"],
            "experiment": row["exp_name"],
            "sample": row["sample_name"],
            "run_name": row["run_name"],
            "timestamp": _epoch_to_iso(row["completed_timestamp"]),
        }

    return json.dumps({
        "run_a": _summary(row_a),
        "run_b": _summary(row_b),
        "swept_diff": _diff_param_lists(params_a["swept"], params_b["swept"]),
        "measured_diff": _diff_param_lists(params_a["measured"], params_b["measured"]),
    })


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------


def register(ctx) -> None:
    """Register all three QCoDeS tools with Hermes Agent."""
    ctx.register_tool(
        name="qcodes_search",
        toolset="qnoe-lab",
        schema=QCODES_SEARCH_SCHEMA,
        handler=_make_handler(_handle_qcodes_search),
        description="Search QCoDeS measurement registry",
    )
    ctx.register_tool(
        name="qcodes_run_details",
        toolset="qnoe-lab",
        schema=QCODES_RUN_DETAILS_SCHEMA,
        handler=_make_handler(_handle_qcodes_run_details),
        description="Get swept/measured parameter details for a QCoDeS run",
    )
    ctx.register_tool(
        name="qcodes_run_diff",
        toolset="qnoe-lab",
        schema=QCODES_RUN_DIFF_SCHEMA,
        handler=_make_handler(_handle_qcodes_run_diff),
        description="Diff swept/measured parameters between two QCoDeS runs",
    )


def _make_handler(fn: Callable) -> Callable:
    """Wrap a handler so it accepts both positional args dict and **kwargs."""
    def handler(args: Dict[str, Any] = None, **kwargs) -> str:
        return fn(args if args is not None else kwargs)
    return handler
