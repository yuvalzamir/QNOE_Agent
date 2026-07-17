#!/usr/bin/env python3
"""SharePoint coverage audit — present-vs-indexed reconciliation.

The CIFS server corpus was silently ~2/3 unindexed for months (mistakes M58)
because no reconciliation existed between what's PRESENT and what's INDEXED.
`scripts/coverage_audit.py` closed that for the server; this closes the same
blind spot for SharePoint, whose manifest (memory/sharepoint.db) is an etag
dedup table, not a coverage check.

For each configured site/drive it compares:
  * PRESENT  = files enumerable via Graph, passing the SAME filters the sync
               applies (extensions, exclude_folders, path substrings,
               max_file_mb) — i.e. "the sync should have indexed this", and
  * INDEXED  = rows in the sp_manifest for that site,
flags per-top-folder coverage below --min-coverage, and additionally reports:
  * document libraries in configured sites that are NOT in the config,
  * tenant sites visible to the credential that are NOT in the config,
  * sites/libraries the credential is DENIED (401/403),
  * manifest-vs-Qdrant divergence (manifest says indexed, points absent),
  * orphans (indexed but no longer present on SharePoint).

READ-ONLY: Graph GETs, an immutable SQLite read, Qdrant count queries.
No Docling, no embedding — safe to run alongside a heavy ingest.

Must run as qnoe-ai (owns secrets/sharepoint.env, which provides
SHAREPOINT_USERNAME / SHAREPOINT_PASSWORD):

  set -a; source /opt/qnoe-agent/secrets/sharepoint.env; set +a
  PYTHONPATH=/opt/qnoe-agent /opt/qnoe-agent/venv/bin/python \
      /opt/qnoe-agent/scripts/sharepoint_coverage_audit.py [--json|--line]
"""
import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import requests
import yaml

# Allow running both as a module and as a bare script.
try:
    from agent.ingest.sharepoint_client import GRAPH_BASE, authenticate, get_site_id
except ModuleNotFoundError:
    sys.path.insert(0, os.environ.get("QNOE_ROOT", "/opt/qnoe-agent"))
    from agent.ingest.sharepoint_client import GRAPH_BASE, authenticate, get_site_id

logger = logging.getLogger("sp_coverage_audit")

# Mirror sharepoint_sync's ingest filters EXACTLY, so "present" means "the
# sync would have indexed it" (apples-to-apples; an intentionally excluded
# file must not count as a gap). Deliberately NOT imported from
# sharepoint_sync — that module pulls in the chunk/embed stack, which is
# heavy and competes for RAM with a running ingest.
SUPPORTED_EXTENSIONS = {".py", ".ipynb", ".md", ".rst", ".pdf", ".pptx", ".docx"}
EXCLUDE_PATH_SUBSTRINGS = {".env/", "/venv/", "site-packages/", "node_modules/",
                           "__pycache__/", ".ipynb_checkpoints/"}

SP_CONFIG_PATH = os.environ.get("SHAREPOINT_CONFIG", "/opt/qnoe-agent/config/sharepoint.yaml")
SP_MANIFEST_DB = os.environ.get("SP_MANIFEST_DB", "/opt/qnoe-agent/memory/sharepoint.db")
QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333").rstrip("/")
TOKEN_REFRESH_SECONDS = 45 * 60  # tokens expire at 60 min
MISSING_SAMPLE_N = 5


class _Token:
    """Token holder that re-authenticates every 45 min (a full noe-group
    listing is thousands of pages and can outlive one token)."""

    def __init__(self, auth_cfg: dict) -> None:
        self._auth_cfg = auth_cfg
        self._token = authenticate(auth_cfg)
        self._ts = time.monotonic()

    def get(self) -> str:
        if time.monotonic() - self._ts >= TOKEN_REFRESH_SECONDS:
            try:
                self._token = authenticate(self._auth_cfg)
                self._ts = time.monotonic()
                logger.info("token refreshed")
            except Exception as exc:
                logger.warning("token refresh failed, using old token: %s", exc)
        return self._token


def _get(url: str, tok: _Token, params: dict | None = None) -> dict:
    """GET with Graph throttling/backoff handling (429/5xx + Retry-After)."""
    last = None
    for _ in range(5):
        last = requests.get(
            url, headers={"Authorization": f"Bearer {tok.get()}"},
            params=params, timeout=60,
        )
        if last.status_code == 429 or last.status_code >= 500:
            wait = min(int(last.headers.get("Retry-After") or 5), 60)
            logger.warning("Graph %d — backing off %ds", last.status_code, wait)
            time.sleep(wait)
            continue
        last.raise_for_status()
        return last.json()
    last.raise_for_status()
    return {}  # unreachable


# --------------------------------------------------------------------------
# PRESENT — stream the Graph listing (never accumulate 900K item dicts)
# --------------------------------------------------------------------------

def _item_path(item: dict) -> str:
    """Relative path from parentReference.path + name (verbatim from
    sharepoint_sync._item_path — manifest item_path rows must match)."""
    parent_path = item.get("parentReference", {}).get("path", "")
    if "root:" in parent_path:
        parent_path = parent_path.split("root:", 1)[1].lstrip("/")
    return f"{parent_path}/{item['name']}".lstrip("/") if parent_path else item["name"]


def iter_drive_files(drive_id: str, tok: _Token):
    """Yield live file items one page at a time via the delta endpoint (the
    same listing path full_sync uses), without storing the whole listing."""
    url = f"{GRAPH_BASE}/drives/{drive_id}/root/delta"
    page = 0
    while url:
        data = _get(url, tok)
        page += 1
        if page % 20 == 0:
            logger.info("listing page %d (drive %s…)", page, drive_id[:20])
        for item in data.get("value", []):
            if "file" in item and "deleted" not in item:
                yield item
        url = data.get("@odata.nextLink")


def present_paths_for_drive(drive_id: str, site_cfg: dict, tok: _Token,
                            dump_fh=None, site_name: str = "") -> set[str]:
    """All rel-paths in the drive that pass the sync's ingest filters.

    When dump_fh is given, EVERY live file item (pre-filter) is appended to it
    as {"site", "id", "path"} JSONL — the live-item inventory consumed by
    scripts/sp_orphan_sweep.py, so the sweep needs no second 45-min listing.
    """
    max_bytes = site_cfg.get("max_file_mb", 50) * 1024 * 1024
    excludes = [e.lstrip("/") for e in site_cfg.get("exclude_folders", [])]
    present: set[str] = set()
    for item in iter_drive_files(drive_id, tok):
        rel = _item_path(item)
        if dump_fh is not None:
            dump_fh.write(json.dumps(
                {"site": site_name, "id": item.get("id", ""), "path": rel}) + "\n")
        if Path(item.get("name", "")).suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        if item.get("size", 0) > max_bytes:
            continue
        if any(rel.startswith(e) for e in excludes):
            continue
        if any(p in rel for p in EXCLUDE_PATH_SUBSTRINGS):
            continue
        present.add(rel)
    return present


def list_drives(site_id: str, tok: _Token) -> list[dict]:
    data = _get(f"{GRAPH_BASE}/sites/{site_id}/drives", tok)
    return data.get("value", [])


# --------------------------------------------------------------------------
# INDEXED — sp_manifest (immutable read: M52 — a plain ro open of this
# yzamir-owned WAL db by another uid leaves a cross-UID -shm that breaks the
# nightly writer; immutable=1 creates no side-files and takes no lock)
# --------------------------------------------------------------------------

def indexed_paths_by_site() -> dict[str, set[str]]:
    conn = sqlite3.connect(f"file:{SP_MANIFEST_DB}?mode=ro&immutable=1", uri=True)
    out: dict[str, set[str]] = defaultdict(set)
    try:
        for site, path in conn.execute("SELECT site_name, item_path FROM sp_manifest"):
            out[site].add(path)
    finally:
        conn.close()
    return out


# --------------------------------------------------------------------------
# Qdrant cross-check — manifest says indexed; are the points actually there?
# --------------------------------------------------------------------------

def qdrant_points_for_repo(collection: str, repo: str) -> int | None:
    try:
        r = requests.post(
            f"{QDRANT_URL}/collections/{collection}/points/count",
            json={"exact": True,
                  "filter": {"must": [{"key": "repo", "match": {"value": repo}}]}},
            timeout=30,
        )
        r.raise_for_status()
        return int(r.json()["result"]["count"])
    except Exception as exc:
        logger.warning("Qdrant count failed for %s/%s: %s", collection, repo, exc)
        return None


# --------------------------------------------------------------------------
# Tenant-wide discovery — gap class 1 (sites never enumerated at all)
# --------------------------------------------------------------------------

def discover_tenant_sites(tok: _Token) -> list[dict]:
    sites: list[dict] = []
    url = f"{GRAPH_BASE}/sites?search=*"
    while url:
        data = _get(url, tok)
        sites.extend(data.get("value", []))
        url = data.get("@odata.nextLink")
    return sites


# --------------------------------------------------------------------------
# Audit
# --------------------------------------------------------------------------

def _top_folder(rel: str) -> str:
    return rel.split("/", 1)[0] if "/" in rel else "(root)"


def audit(cfg: dict, tok: _Token, min_cov: float, only_site: str | None,
          skip_tenant_scan: bool, dump_live: str | None = None) -> dict:
    indexed_all = indexed_paths_by_site()
    result: dict = {"sites": [], "tenant": None, "denied": [],
                    "min_coverage": min_cov}

    site_cfgs = cfg["sites"]
    if only_site:
        site_cfgs = [s for s in site_cfgs if s["name"] == only_site]

    # The dump is written alternately by the qnoe-ai on-demand unit and the
    # yzamir nightly task; a leftover file owned by the other uid blocks a
    # plain "w" open. Both uids have write on logs/, so unlink-then-create.
    dump_fh = None
    if dump_live:
        try:
            os.unlink(dump_live)
        except FileNotFoundError:
            pass
        dump_fh = open(dump_live, "w")
        os.chmod(dump_live, 0o664)
    configured_site_ids: set[str] = set()

    for site_cfg in site_cfgs:
        name = site_cfg["name"]
        entry: dict = {"site": name, "drives": [], "unconfigured_libraries": [],
                       "folders": [], "orphans": 0, "qdrant": None}
        result["sites"].append(entry)
        try:
            site_id = get_site_id(site_cfg["teams_group_id"], tok.get())
            configured_site_ids.add(site_id)
            drives = list_drives(site_id, tok)
        except requests.HTTPError as exc:
            code = exc.response.status_code if exc.response is not None else "?"
            result["denied"].append({"site": name, "stage": "site/drives", "status": code})
            logger.error("site %s: drive listing denied/failed (%s)", name, code)
            continue

        configured_drives = {d.lower() for d in site_cfg.get("drives", ["Documents"])}
        entry["unconfigured_libraries"] = [
            d["name"] for d in drives if d["name"].lower() not in configured_drives
        ]

        present: set[str] = set()
        for d in drives:
            if d["name"].lower() not in configured_drives:
                continue
            logger.info("enumerating %s / %s …", name, d["name"])
            try:
                paths = present_paths_for_drive(d["id"], site_cfg, tok,
                                                dump_fh=dump_fh, site_name=name)
            except requests.HTTPError as exc:
                code = exc.response.status_code if exc.response is not None else "?"
                result["denied"].append({"site": name, "stage": f"drive:{d['name']}", "status": code})
                logger.error("site %s drive %s: enumeration denied/failed (%s)", name, d["name"], code)
                continue
            entry["drives"].append({"drive": d["name"], "present": len(paths)})
            present |= paths

        indexed = indexed_all.get(name, set())
        missing = present - indexed
        entry["orphans"] = len(indexed - present)  # indexed but gone from SP

        p_cnt = Counter(_top_folder(x) for x in present)
        i_cnt = Counter(_top_folder(x) for x in (present & indexed))
        miss_by_folder: dict[str, list[str]] = defaultdict(list)
        for m in sorted(missing):
            f = _top_folder(m)
            if len(miss_by_folder[f]) < MISSING_SAMPLE_N:
                miss_by_folder[f].append(m)

        for folder in sorted(p_cnt):
            p, i = p_cnt[folder], i_cnt.get(folder, 0)
            cov = i / p if p else None
            gap = p > 0 and (cov is None or cov < min_cov)
            entry["folders"].append({
                "folder": folder, "present": p, "indexed": i,
                "coverage": round(cov, 3) if cov is not None else None,
                "gap": bool(gap),
                "missing_sample": miss_by_folder.get(folder, []) if gap else [],
            })
        entry["folders"].sort(key=lambda r: (not r["gap"], -(r["present"] - r["indexed"])))
        entry["present_total"] = len(present)
        entry["indexed_total"] = len(indexed)
        entry["missing_total"] = len(missing)

        points = qdrant_points_for_repo(site_cfg["collection"], name)
        entry["qdrant"] = {
            "collection": site_cfg["collection"], "points": points,
            "manifest_rows": len(indexed),
            # manifest says indexed but no points at all = purge/half-write
            "divergence": bool(len(indexed) > 0 and points == 0),
        }

    if not skip_tenant_scan:
        try:
            tenant = discover_tenant_sites(tok)
            unconfigured = [
                {"name": s.get("displayName") or s.get("name", "?"),
                 "webUrl": s.get("webUrl", "")}
                for s in tenant if s.get("id") not in configured_site_ids
            ]
            result["tenant"] = {
                "visible_sites": len(tenant),
                "configured_matched": len(configured_site_ids),
                "unconfigured_sites": unconfigured,
            }
        except requests.HTTPError as exc:
            code = exc.response.status_code if exc.response is not None else "?"
            result["denied"].append({"site": "<tenant scan>", "stage": "sites?search=*", "status": code})
            logger.warning("tenant site discovery denied/failed (%s)", code)

    if dump_fh is not None:
        dump_fh.close()
        logger.info("live-item dump written: %s", dump_live)
    return result


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def summary_line(res: dict) -> str:
    gapped = [(s["site"], f) for s in res["sites"] for f in s["folders"] if f["gap"]]
    diverged = [s["site"] for s in res["sites"]
                if s.get("qdrant") and s["qdrant"]["divergence"]]
    unconf_sites = len((res.get("tenant") or {}).get("unconfigured_sites", []))
    bits = []
    if not gapped:
        tot_p = sum(s.get("present_total", 0) for s in res["sites"])
        tot_i = sum(s.get("indexed_total", 0) for s in res["sites"])
        bits.append(f"all folders >= {int(res['min_coverage']*100)}% ✅ ({tot_i}/{tot_p})")
    else:
        worst = "; ".join(f"{s}:{f['folder']} {f['indexed']}/{f['present']}"
                          for s, f in gapped[:4])
        bits.append(f"⚠️ {len(gapped)} folder(s) under {int(res['min_coverage']*100)}% — {worst}")
    if diverged:
        bits.append(f"⚠️ manifest/Qdrant divergence: {', '.join(diverged)}")
    if res["denied"]:
        bits.append(f"⚠️ {len(res['denied'])} denied enumeration(s)")
    if unconf_sites:
        bits.append(f"{unconf_sites} tenant site(s) not in config")
    return "SP coverage audit: " + " · ".join(bits)


def print_table(res: dict) -> None:
    for s in res["sites"]:
        print(f"\n== {s['site']} — present {s.get('present_total', 0)}, "
              f"indexed {s.get('indexed_total', 0)}, missing {s.get('missing_total', 0)}, "
              f"orphans {s['orphans']}")
        if s.get("qdrant"):
            q = s["qdrant"]
            print(f"   qdrant[{q['collection']}] repo points: {q['points']}"
                  + ("  <-- DIVERGENCE (manifest rows but 0 points)" if q["divergence"] else ""))
        if s["unconfigured_libraries"]:
            print(f"   libraries NOT in config: {', '.join(s['unconfigured_libraries'])}")
        print(f"   {'folder':<40}{'present':>9}{'indexed':>9}{'cover':>8}")
        for r in s["folders"]:
            cov = "n/a" if r["coverage"] is None else f"{int(r['coverage']*100)}%"
            flag = "  <-- GAP" if r["gap"] else ""
            print(f"   {r['folder'][:39]:<40}{r['present']:>9}{r['indexed']:>9}{cov:>8}{flag}")
            for m in r["missing_sample"]:
                print(f"       missing: {m}")
    t = res.get("tenant")
    if t:
        print(f"\n== tenant: {t['visible_sites']} site(s) visible to the credential, "
              f"{len(t['unconfigured_sites'])} NOT in config:")
        for u in t["unconfigured_sites"]:
            print(f"   {u['name']:<50} {u['webUrl']}")
    if res["denied"]:
        print("\n== credential DENIED:")
        for d in res["denied"]:
            print(f"   {d['site']} @ {d['stage']} (HTTP {d['status']})")
    print("\n" + summary_line(res))


def main(argv) -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    ap = argparse.ArgumentParser(description="SharePoint index coverage audit (read-only)")
    ap.add_argument("--config", default=None, help="path to sharepoint.yaml")
    ap.add_argument("--site", default=None, help="audit only this site (by name)")
    ap.add_argument("--min-coverage", type=float,
                    default=float(os.environ.get("MIN_COVERAGE", "0.8")))
    ap.add_argument("--skip-tenant-scan", action="store_true",
                    help="skip the tenant-wide /sites?search=* discovery")
    ap.add_argument("--dump-live", default=None, metavar="FILE",
                    help="also write every live file item (site,id,path) as JSONL "
                         "— consumed by scripts/sp_orphan_sweep.py")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--line", action="store_true")
    args = ap.parse_args(argv)

    with open(args.config or SP_CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    tok = _Token(cfg["auth"])
    logger.info("authentication OK")

    res = audit(cfg, tok, args.min_coverage, args.site, args.skip_tenant_scan,
                dump_live=args.dump_live)
    if args.json:
        print(json.dumps({**res, "summary": summary_line(res)}))
    elif args.line:
        print(summary_line(res))
    else:
        print_table(res)

    gapped = any(f["gap"] for s in res["sites"] for f in s["folders"])
    diverged = any(s.get("qdrant") and s["qdrant"]["divergence"] for s in res["sites"])
    return 1 if (gapped or diverged or res["denied"]) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
