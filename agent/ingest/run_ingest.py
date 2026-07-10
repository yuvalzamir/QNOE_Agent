"""Ingestion CLI — index a local repo directory into Qdrant.

Usage:
  python -m agent.ingest.run_ingest --team qtm --repo-path /path/to/QTM_CodeBase
  python -m agent.ingest.run_ingest --team photocurrent --repo-path /path/to/SLG07-PhQH
  python -m agent.ingest.run_ingest --team group-wide --repo-path /path/to/papers/
  python -m agent.ingest.run_ingest --team group-wide --file-list /tmp/confirmed.txt --repo-name Manuscripts

Options:
  --team        Target Qdrant collection (qtm | photocurrent | group-wide | ...)
  --repo-path   Path to directory to index (not required when --file-list is used)
  --repo-name   Name to tag chunks with (defaults to directory name)
  --force       Re-index even if file hash is unchanged
  --file-list   Text file of absolute paths to re-index (one per line). Implies --force.
                Use this to re-run Docling on specific files after pypdf-only ingestion.
  --dry-run     Print what would be indexed without writing to Qdrant

Hash-based deduplication: SHA-256 of file content is stored in SQLite
index_manifest table. Unchanged files are skipped on subsequent runs.
"""
import argparse
import hashlib
import logging
import os
import sqlite3
import sys
from pathlib import Path
from uuid import uuid4

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct, SparseVector, SparseVectorParams, SparseIndexParams

from .splitter import chunk_file
from .embed import embed_documents, embed_sparse, VECTOR_DIM
from .excluded import find_prune_args

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
AGENT_DATA_DIR = os.environ.get("AGENT_DATA_DIR", "/opt/qnoe-agent/memory")
MANIFEST_DB = os.path.join(AGENT_DATA_DIR, "episodic.db")

SUPPORTED_EXTENSIONS = {".py", ".ipynb", ".md", ".txt", ".rst", ".pdf", ".pptx", ".docx"}
UPSERT_BATCH = 100
SKIPPED_FILES_LOG = Path(os.environ.get("SKIPPED_FILES_LOG", "/tmp/skipped_files.log"))
ONE_CHUNK_LOG = Path(os.environ.get("ONE_CHUNK_LOG", "/tmp/one_chunk_files.log"))
OVERSIZED_FILES_LOG = Path(os.environ.get("OVERSIZED_FILES_LOG", "/tmp/oversized_files.log"))
DOCLING_MAX_FILE_BYTES = int(os.environ.get("DOCLING_MAX_FILE_BYTES", str(50 * 1024 * 1024)))  # 50 MB
DOCLING_EXTENSIONS = {".pdf", ".docx", ".pptx"}


# ── Manifest (hash-based deduplication) ───────────────────────────────────────

def _get_manifest_conn(manifest_db: str = MANIFEST_DB) -> sqlite3.Connection:
    conn = sqlite3.connect(manifest_db)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS index_manifest (
            id           INTEGER PRIMARY KEY,
            file_path    TEXT NOT NULL UNIQUE,
            sha256       TEXT NOT NULL,
            collection   TEXT NOT NULL,
            point_ids    TEXT NOT NULL,   -- JSON list of Qdrant point UUIDs
            indexed_at   TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _is_unchanged(conn: sqlite3.Connection, path: Path, sha256: str) -> bool:
    row = conn.execute(
        "SELECT sha256 FROM index_manifest WHERE file_path = ?", (str(path),)
    ).fetchone()
    return row is not None and row[0] == sha256


def _record_file(
    conn: sqlite3.Connection,
    path: Path,
    sha256: str,
    collection: str,
    point_ids: list[str],
) -> None:
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    import json
    conn.execute(
        """INSERT OR REPLACE INTO index_manifest
           (file_path, sha256, collection, point_ids, indexed_at)
           VALUES (?, ?, ?, ?, ?)""",
        (str(path), sha256, collection, json.dumps(point_ids), now),
    )
    conn.commit()


# ── Qdrant helpers ─────────────────────────────────────────────────────────────

def _ensure_collection(client: QdrantClient, collection: str) -> None:
    existing = [c.name for c in client.get_collections().collections]
    if collection not in existing:
        client.create_collection(
            collection_name=collection,
            vectors_config=VectorParams(size=VECTOR_DIM, distance=Distance.COSINE),
            sparse_vectors_config={"text-sparse": SparseVectorParams(
                index=SparseIndexParams(on_disk=False)
            )},
        )
        logger.info("Created collection: %s", collection)


def _add_sparse_to_collection(client: QdrantClient, collection: str) -> None:
    """Add text-sparse vector field to an existing collection (idempotent)."""
    from qdrant_client.models import SparseVectorNameConfig, SparseVectorConfig
    info = client.get_collection(collection)
    if info.config.params.sparse_vectors and "text-sparse" in info.config.params.sparse_vectors:
        return  # already has sparse field
    try:
        client.create_vector_name(
            collection_name=collection,
            vector_name="text-sparse",
            vector_name_config=SparseVectorNameConfig(sparse=SparseVectorConfig()),
        )
    except Exception as e:
        logger.warning("Could not add sparse field to %s: %s", collection, e)


def _delete_old_chunks(
    client: QdrantClient, conn: sqlite3.Connection, path: Path, collection: str
) -> None:
    """Delete any previously indexed chunks for this file from Qdrant.

    Uses the collection recorded in the manifest (not the current target),
    so chunks are cleaned up correctly if a file moves between collections.
    """
    import json
    row = conn.execute(
        "SELECT collection, point_ids FROM index_manifest WHERE file_path = ?", (str(path),)
    ).fetchone()
    if not row:
        return
    old_collection = row[0]
    old_ids = json.loads(row[1])
    if old_ids:
        try:
            client.delete(collection_name=old_collection, points_selector=old_ids)
        except Exception as exc:
            logger.warning("Could not delete old chunks from %s for %s: %s", old_collection, path.name, exc)


def _upsert_chunks(
    client: QdrantClient,
    collection: str,
    chunks: list[dict],
    vectors: list[list[float]],
    sparse_vecs: list,
) -> list[str]:
    point_ids = [str(uuid4()) for _ in chunks]
    for i in range(0, len(chunks), UPSERT_BATCH):
        batch_slice = slice(i, i + UPSERT_BATCH)
        points = [
            PointStruct(
                id=pid,
                vector={
                    "": vec,
                    "text-sparse": SparseVector(
                        indices=sv.indices.tolist(), values=sv.values.tolist()
                    ),
                },
                payload=chunk,
            )
            for pid, vec, sv, chunk in zip(
                point_ids[batch_slice], vectors[batch_slice],
                sparse_vecs[batch_slice], chunks[batch_slice],
            )
        ]
        client.upsert(collection_name=collection, points=points)
    return point_ids


# ── File discovery ─────────────────────────────────────────────────────────────

def _find_files(root: Path) -> list[Path]:
    """Return all indexable files under root using the OS `find` command.

    Much faster than pathlib.rglob() on CIFS/network mounts because `find`
    batches directory reads in the kernel rather than issuing a stat() syscall
    per file from Python.

    Set EXCLUDE_EXTENSIONS=.txt,.rst (comma-separated) to skip certain types.
    Useful for server ingestion where .txt files are raw measurement data.
    """
    import subprocess
    exclude = {
        e.strip().lower() if e.strip().startswith(".") else f".{e.strip().lower()}"
        for e in os.environ.get("EXCLUDE_EXTENSIONS", "").split(",")
        if e.strip()
    }
    exts = SUPPORTED_EXTENSIONS - exclude
    # Build: -name "*.py" -o -name "*.ipynb" -o ...
    name_exprs = []
    for ext in exts:
        name_exprs += ["-o", "-name", f"*{ext}"]
    name_exprs = name_exprs[1:]  # drop leading -o

    cmd = [
        "find", str(root),
        *find_prune_args(),
        "-type", "f",
        "!", "-name", "~$*",       # skip Windows Office lock files
        "!", "-path", "*/.git/*",  # skip git internals
        "(", *name_exprs, ")",
        "-print",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        paths = [Path(p) for p in result.stdout.splitlines() if p.strip()]
        # Case-insensitive extension filter (find -name is case-sensitive on Linux)
        return [p for p in paths if p.suffix.lower() in exts]
    except subprocess.TimeoutExpired:
        logger.warning("find timed out on %s — falling back to rglob", root)
        return [
            p for p in root.rglob("*")
            if p.is_file() and p.suffix.lower() in exts
            and ".git" not in p.parts
            and not p.name.startswith("~$")
        ]


# ── Main ───────────────────────────────────────────────────────────────────────

def ingest_directory(
    team: str,
    repo_path: Path,
    repo_name: str,
    force: bool = False,
    dry_run: bool = False,
    force_extensions: set[str] | None = None,
    file_list: list[Path] | None = None,
    manifest_db: str | None = None,
) -> None:
    client = QdrantClient(url=QDRANT_URL)
    _ensure_collection(client, team)
    conn = _get_manifest_conn(manifest_db or MANIFEST_DB)

    if file_list is not None:
        files = sorted(file_list)
        logger.info("Processing %d files from --file-list", len(files))
        force = True  # always re-index when given explicit list
    else:
        files = sorted(_find_files(repo_path))
        logger.info("Found %d indexable files in %s", len(files), repo_path)

    total_chunks = 0
    skipped = 0
    new_files = 0
    updated_files = 0

    for path in files:
        try:
            sha256 = _file_hash(path)
        except Exception as exc:
            logger.warning("Could not hash %s: %s", path, exc)
            try:
                with open(SKIPPED_FILES_LOG, "a", encoding="utf-8") as f:
                    f.write(f"{path}\t{exc}\n")
            except Exception:
                pass
            continue

        force_this = force or (force_extensions and path.suffix.lower() in force_extensions)
        if not force_this and _is_unchanged(conn, path, sha256):
            skipped += 1
            continue

        # Skip empty files and Git LFS pointers — record in manifest so they
        # are not retried every night until the file actually changes on disk.
        try:
            fsize = path.stat().st_size
        except OSError:
            fsize = -1
        if fsize == 0:
            logger.info("Empty file, skipping: %s", path.name)
            _record_file(conn, path, sha256, team, [])
            skipped += 1
            continue
        if fsize < 200 and path.suffix.lower() in DOCLING_EXTENSIONS:
            try:
                first_line = path.read_bytes()[:40].decode("utf-8", errors="ignore")
            except OSError:
                first_line = ""
            if first_line.startswith("version https://git-lfs"):
                logger.info("Git LFS pointer, skipping: %s", path.name)
                _record_file(conn, path, sha256, team, [])
                skipped += 1
                continue

        # Skip oversized Docling files (memory safety)
        if path.suffix.lower() in DOCLING_EXTENSIONS:
            try:
                fsize = path.stat().st_size
            except OSError:
                fsize = 0
            if fsize > DOCLING_MAX_FILE_BYTES:
                logger.warning("Oversized (%d MB): %s", fsize // (1024 * 1024), path)
                try:
                    with open(OVERSIZED_FILES_LOG, "a", encoding="utf-8") as f:
                        f.write(f"{path}\t{fsize}\n")
                except Exception:
                    pass
                continue

        # Track whether this is a new file or an update
        is_new = conn.execute(
            "SELECT 1 FROM index_manifest WHERE file_path = ?", (str(path),)
        ).fetchone() is None
        if is_new:
            new_files += 1
        else:
            updated_files += 1

        # Delete old chunks from Qdrant before re-indexing
        _delete_old_chunks(client, conn, path, team)

        chunks = chunk_file(path, repo_name)
        if not chunks:
            continue

        if len(chunks) == 1:
            try:
                with open(ONE_CHUNK_LOG, "a", encoding="utf-8") as f:
                    f.write(f"{path}\n")
            except Exception:
                pass

        if dry_run:
            logger.info("[DRY-RUN] %s → %d chunks", path, len(chunks))
            total_chunks += len(chunks)
            continue

        texts = [c["text"] for c in chunks]
        vectors = embed_documents(texts)
        sparse_vecs = embed_sparse(texts)
        point_ids = _upsert_chunks(client, team, chunks, vectors, sparse_vecs)
        _record_file(conn, path, sha256, team, point_ids)
        total_chunks += len(chunks)
        logger.info("Indexed %s → %d chunks", path.name, len(chunks))

    logger.info(
        "Done. Indexed %d chunks from %d files (%d new, %d updated, %d skipped unchanged).",
        total_chunks, new_files + updated_files, new_files, updated_files, skipped,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Index a repo into Qdrant")
    parser.add_argument("--team", required=True, help="Qdrant collection name")
    parser.add_argument("--repo-path", default=None, help="Directory to index (not required with --file-list)")
    parser.add_argument("--repo-name", default=None, help="Tag for chunks (default: dir name)")
    parser.add_argument("--force", action="store_true", help="Re-index unchanged files")
    parser.add_argument("--force-ext", metavar="EXT", nargs="+", help="Re-index only files with these extensions (e.g. --force-ext .pdf .docx)")
    parser.add_argument("--file-list", metavar="FILE", default=None, help="Text file of absolute paths to re-index (one per line). Implies --force.")
    parser.add_argument("--dry-run", action="store_true", help="Print without writing")
    args = parser.parse_args()

    file_list = None
    if args.file_list:
        list_path = Path(args.file_list)
        if not list_path.is_file():
            logger.error("--file-list not found: %s", list_path)
            sys.exit(1)
        file_list = [
            Path(line.strip()) for line in list_path.read_text().splitlines()
            if line.strip() and not line.startswith("#")
        ]
        missing = [p for p in file_list if not p.exists()]
        if missing:
            logger.warning("%d paths in file-list not found on disk (will skip):", len(missing))
            for p in missing[:10]:
                logger.warning("  %s", p)
        file_list = [p for p in file_list if p.exists()]
        repo_path = Path(args.repo_path).resolve() if args.repo_path else Path("/")
        repo_name = args.repo_name or "confirmed_papers"
    else:
        if not args.repo_path:
            logger.error("--repo-path is required when --file-list is not used")
            sys.exit(1)
        repo_path = Path(args.repo_path).resolve()
        if not repo_path.is_dir():
            logger.error("Not a directory: %s", repo_path)
            sys.exit(1)
        repo_name = args.repo_name or repo_path.name

    force_extensions = {e if e.startswith(".") else f".{e}" for e in args.force_ext} if args.force_ext else None
    ingest_directory(args.team, repo_path, repo_name, args.force, args.dry_run, force_extensions, file_list)


def _file_accessible(path: Path) -> bool:
    """Check if a file is accessible on disk (handles network errors, permissions, etc.)."""
    try:
        path.stat()
        return True
    except (OSError, PermissionError):
        return False


def sweep_orphans(manifest_db: str, qdrant_url: str, grace_days: int = 7) -> dict:
    """Remove Qdrant chunks for files missing from disk for grace_days+ days.

    Tracks first-seen-missing timestamps to avoid false positives from
    transient mount failures or network errors.

    Returns stats dict with keys: checked, newly_missing, still_missing, recovered, deleted.
    """
    import json
    from datetime import datetime, timezone

    conn = sqlite3.connect(manifest_db)
    # Ensure missing_files table exists
    conn.execute("""
        CREATE TABLE IF NOT EXISTS missing_files (
            file_path    TEXT PRIMARY KEY,
            first_seen   TEXT NOT NULL,
            last_checked TEXT NOT NULL
        )
    """)
    conn.commit()

    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    stats = {"checked": 0, "newly_missing": 0, "still_missing": 0, "recovered": 0, "deleted": 0}

    rows = conn.execute("SELECT id, file_path, collection, point_ids FROM index_manifest").fetchall()
    stats["checked"] = len(rows)

    client = QdrantClient(url=qdrant_url)

    for row_id, file_path, collection, point_ids_json in rows:
        path = Path(file_path)

        if _file_accessible(path):
            # File is back — clear any missing mark
            deleted_rows = conn.execute(
                "DELETE FROM missing_files WHERE file_path = ?", (file_path,)
            ).rowcount
            if deleted_rows:
                conn.commit()
                stats["recovered"] += 1
            continue

        # File is inaccessible
        existing = conn.execute(
            "SELECT first_seen FROM missing_files WHERE file_path = ?", (file_path,)
        ).fetchone()

        if existing is None:
            conn.execute(
                "INSERT INTO missing_files (file_path, first_seen, last_checked) VALUES (?, ?, ?)",
                (file_path, now_iso, now_iso),
            )
            conn.commit()
            stats["newly_missing"] += 1
            continue

        # Already tracked — update last_checked
        first_seen = datetime.fromisoformat(existing[0])
        conn.execute(
            "UPDATE missing_files SET last_checked = ? WHERE file_path = ?",
            (now_iso, file_path),
        )
        conn.commit()

        if (now - first_seen).days >= grace_days:
            # Grace period expired — delete from Qdrant + manifest
            point_ids = json.loads(point_ids_json) if point_ids_json else []
            if point_ids:
                try:
                    client.delete(collection_name=collection, points_selector=point_ids)
                except Exception as exc:
                    logger.warning("Could not delete Qdrant points for %s: %s", file_path, exc)
            conn.execute("DELETE FROM index_manifest WHERE id = ?", (row_id,))
            conn.execute("DELETE FROM missing_files WHERE file_path = ?", (file_path,))
            conn.commit()
            stats["deleted"] += 1
            logger.info("Orphan deleted: %s (%d points from %s)", file_path, len(point_ids), collection)
        else:
            stats["still_missing"] += 1

    conn.close()
    return stats


if __name__ == "__main__":
    main()
