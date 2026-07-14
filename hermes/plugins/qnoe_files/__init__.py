"""QNOE file-locator plugin for Hermes Agent.

Exposes a single tool, ``find_file``, that answers "where is the file called
X?" across BOTH storage backends the agent knows about:

  * CIFS lab data server + GitHub repos  — ``index_manifest`` tables
    (populated by the ingestion pipeline / nightly re-index).
  * SharePoint document libraries         — ``sp_manifest`` table
    (populated by ``sharepoint_sync``; ``web_url`` gives a clickable link).

All lookups are local SQLite ``LIKE`` queries against the ingestion manifests
— fast, offline, no filesystem walk (a live ``find`` over the CIFS mount takes
hours) and no Graph API call. Coverage is therefore whatever has been indexed:
supported file types, non-excluded paths. Files that were never ingested
(e.g. ``.xlsx``, images) will not appear.

Returns, per hit:
    source     "cifs" | "sharepoint"
    path       relative/full path
    site       SharePoint site name (SP hits only)
    link       filesystem path (CIFS) or web URL (SharePoint)
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from typing import Any, Callable, Dict, List

logger = logging.getLogger(__name__)

# CIFS / repo file manifests. Each is an episodic.db holding an index_manifest
# table (file_path column). The server DB indexes /ICFO/groups/NOE; the repo DB
# indexes the cloned GitHub repos. Both are searched; either may be absent.
CIFS_MANIFEST_DBS = [
    p.strip()
    for p in os.environ.get(
        "CIFS_MANIFEST_DBS",
        "/home/yzamir/qnoe_server_data/episodic.db:/opt/qnoe-agent/memory/episodic.db",
    ).split(":")
    if p.strip()
]

SP_MANIFEST_DB = os.environ.get("SP_MANIFEST_DB", "/opt/qnoe-agent/memory/sharepoint.db")

DEFAULT_LIMIT = 25
MAX_LIMIT = 100

FIND_FILE_SCHEMA = {
    "name": "find_file",
    "description": (
        "Locate a file/document/notebook/script by (part of) its name or folder "
        "path. THIS IS THE ONLY WAY TO FIND SHAREPOINT DOCUMENTS: SharePoint files "
        "are NOT on the local filesystem (so search_files/terminal/list_directory "
        "cannot see them), but they ARE indexed here — this tool returns their "
        "web link. Always use find_file for any 'where is X' / 'find the document "
        "about X' / locate request, covering BOTH the lab CIFS server and "
        "SharePoint. Case-insensitive substring match on file name + folder path. "
        "Indexed files only."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Substring to match against file name and folder path.",
            },
            "source": {
                "type": "string",
                "enum": ["all", "cifs", "sharepoint"],
                "description": "Which store to search. Default 'all'.",
            },
            "limit": {
                "type": "integer",
                "description": f"Max results (default {DEFAULT_LIMIT}, max {MAX_LIMIT}).",
            },
        },
        "required": ["query"],
    },
}


def _search_cifs(query: str, limit: int) -> List[Dict[str, Any]]:
    like = f"%{query}%"
    results: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for db_path in CIFS_MANIFEST_DBS:
        if not os.path.exists(db_path):
            continue
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        except sqlite3.OperationalError:
            continue
        try:
            # Table may not exist in every DB — guard with try.
            rows = conn.execute(
                """SELECT file_path, collection FROM index_manifest
                    WHERE file_path LIKE ? ORDER BY file_path LIMIT ?""",
                (like, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
        finally:
            conn.close()
        for file_path, collection in rows:
            if file_path in seen:
                continue
            seen.add(file_path)
            results.append(
                {
                    "source": "cifs",
                    "path": file_path,
                    "collection": collection,
                    "link": file_path,
                }
            )
    return results


def _search_sharepoint(query: str, limit: int) -> List[Dict[str, Any]]:
    if not os.path.exists(SP_MANIFEST_DB):
        return []
    like = f"%{query}%"
    try:
        conn = sqlite3.connect(f"file:{SP_MANIFEST_DB}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        return []
    try:
        rows = conn.execute(
            """SELECT item_path, site_name, web_url, collection FROM sp_manifest
                WHERE item_path LIKE ? ORDER BY item_path LIMIT ?""",
            (like, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        conn.close()
    results: List[Dict[str, Any]] = []
    for item_path, site_name, web_url, collection in rows:
        results.append(
            {
                "source": "sharepoint",
                "path": item_path,
                "site": site_name,
                "collection": collection,
                # web_url may be NULL for rows not yet backfilled — fall back to path.
                "link": web_url or item_path,
            }
        )
    return results


def _handle_find_file(args: Dict[str, Any]) -> str:
    query = (args.get("query") or "").strip()
    if not query:
        return json.dumps({"error": "query is required."})

    source = (args.get("source") or "all").lower()
    try:
        limit = int(args.get("limit", DEFAULT_LIMIT))
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    limit = min(max(1, limit), MAX_LIMIT)

    results: List[Dict[str, Any]] = []
    try:
        if source in ("all", "cifs"):
            results.extend(_search_cifs(query, limit))
        if source in ("all", "sharepoint"):
            results.extend(_search_sharepoint(query, limit))
    except Exception as exc:  # never crash the tool loop
        logger.exception("find_file failed")
        return json.dumps({"error": str(exc)})

    truncated = len(results) > limit
    results = results[:limit]
    return json.dumps(
        {
            "count": len(results),
            "truncated": truncated,
            "results": results,
        }
    )


def register(ctx) -> None:
    """Register the find_file tool with Hermes Agent."""
    ctx.register_tool(
        name="find_file",
        toolset="qnoe-lab",
        schema=FIND_FILE_SCHEMA,
        handler=_make_handler(_handle_find_file),
        description="Locate a file by name/path across CIFS and SharePoint",
    )


def _make_handler(fn: Callable) -> Callable:
    """Wrap a handler so it accepts both a positional args dict and **kwargs."""

    def handler(args: Dict[str, Any] = None, **kwargs) -> str:
        return fn(args if args is not None else kwargs)

    return handler
