"""SMB3 file watcher daemon — continuous change detection via CIFS_IOC_NOTIFY.

Architecture:
  Main thread
   |-- MountMonitor thread       (every 60s: detect mount drop/restore)
   |-- FolderWatcher thread x N  (one per watched folder/subfolder)
   |-- SubfolderManager thread x M  (manages FolderWatchers for Projects/Notebook children)
   |-- StabilityChecker thread   (every 10 min: re-stat queued files)
   +-- CacheRebuilder thread     (every 24h OR on remount)

Usage:
  python -m agent.watcher.smb_watcher                 # run daemon
  python -m agent.watcher.smb_watcher --rebuild-cache  # one-shot full rebuild
  python -m agent.watcher.smb_watcher --dump-queue     # print pending queue
"""
import argparse
import fcntl
import logging
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from struct import pack

import yaml

from .file_cache import (
    cleanup_old_entries,
    get_pending_queue,
    init_schema,
    mark_stable_files,
    update_cache_and_queue,
)

try:
    from agent.ingest.sharepoint_sync import (
        delta_sync as _sp_delta_sync,
        load_sharepoint_config as _sp_load_config,
    )
    from agent.ingest.sharepoint_client import authenticate as _sp_authenticate
    _SP_AVAILABLE = True
except ImportError:
    _SP_AVAILABLE = False

logger = logging.getLogger(__name__)

CIFS_IOC_NOTIFY = 0x4005CF09
CF_ALL = 0x17F  # all change types (file + dir)
CF_DIR_CHANGES = 0x003  # FILE_NOTIFY_CHANGE_DIR_NAME only


def _load_config(config_path: str | None = None) -> dict:
    path = config_path or os.environ.get(
        "WATCHER_CONFIG", "/opt/qnoe-agent/config/watcher.yaml"
    )
    with open(path) as f:
        return yaml.safe_load(f)


def _get_conn(config: dict) -> sqlite3.Connection:
    conn = sqlite3.connect(config["db_path"], check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    init_schema(conn)
    return conn


def _targeted_find(
    folder: Path, supported_exts: set[str], exclude_path_substrings: list[str] | None = None,
) -> dict[str, tuple[int, int, str]]:
    """Run `find` on a single folder, return {path: (mtime_ns, size, ext)}."""
    name_exprs: list[str] = []
    for ext in supported_exts:
        name_exprs += ["-o", "-name", f"*{ext}"]
    name_exprs = name_exprs[1:]  # drop leading -o

    exclude_exprs: list[str] = []
    for substr in (exclude_path_substrings or []):
        exclude_exprs += ["!", "-path", f"*{substr}*"]

    cmd = [
        "find", str(folder),
        "-type", "f",
        "!", "-name", "~$*",
        "!", "-path", "*/.git/*",
        "!", "-iname", "Thumbs.db",
        *exclude_exprs,
        "(", *name_exprs, ")",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        logger.warning("find timed out on %s", folder)
        return {}

    files: dict[str, tuple[int, int, str]] = {}
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        p = Path(line)
        ext = p.suffix.lower()
        if ext not in supported_exts:
            continue
        try:
            stat = p.stat()
            files[line] = (stat.st_mtime_ns, stat.st_size, ext)
        except OSError:
            continue
    return files


# ---------------------------------------------------------------------------
# Thread classes
# ---------------------------------------------------------------------------


class FolderWatcher(threading.Thread):
    """Watches a single folder via CIFS_IOC_NOTIFY. Queues changes on notification."""

    daemon = True

    def __init__(
        self, folder: Path, config: dict, stop_event: threading.Event,
        db_lock: threading.Lock,
    ):
        super().__init__(name=f"Watch-{folder.name}")
        self._folder = folder
        self._config = config
        self._stop = stop_event
        self._db_lock = db_lock
        self._exts = set(config["supported_extensions"]["docs"] + config["supported_extensions"]["dbs"])
        self._cooldown = config.get("scan_cooldown_seconds", 5)

    def run(self):
        # Seed cache on first start (no enqueue)
        self._scan_and_update()

        while not self._stop.is_set():
            fd = None
            try:
                fd = os.open(str(self._folder), os.O_RDONLY | os.O_DIRECTORY)
                # Blocks until something changes in this subtree
                fcntl.ioctl(fd, CIFS_IOC_NOTIFY, pack("=IB", CF_ALL, 1))
                os.close(fd)
                fd = None

                if self._stop.is_set():
                    break

                # Cooldown to batch rapid changes
                time.sleep(self._cooldown)
                self._scan_and_update()

            except OSError as exc:
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                if self._stop.is_set():
                    break
                logger.warning("FolderWatcher %s: %s — retrying in 60s", self._folder.name, exc)
                self._stop.wait(60)

    def _scan_and_update(self):
        current = _targeted_find(self._folder, self._exts, self._config.get("exclude_path_substrings"))
        with self._db_lock:
            conn = _get_conn(self._config)
            stats = update_cache_and_queue(conn, str(self._folder), current)
            conn.close()
        total = stats["new"] + stats["modified"] + stats["deleted"]
        if total > 0:
            logger.info(
                "FolderWatcher %s: +%d ~%d -%d",
                self._folder.name, stats["new"], stats["modified"], stats["deleted"],
            )


class SubfolderManager(threading.Thread):
    """Manages FolderWatcher threads for children of a watch_subfolder_level folder."""

    daemon = True

    def __init__(
        self, parent_folder: Path, exclude: set[str], config: dict,
        stop_event: threading.Event, db_lock: threading.Lock,
    ):
        super().__init__(name=f"SubMgr-{parent_folder.name}")
        self._parent = parent_folder
        self._exclude = exclude
        self._config = config
        self._stop = stop_event
        self._db_lock = db_lock
        self._child_watchers: dict[str, tuple[FolderWatcher, threading.Event]] = {}
        self._cooldown = config.get("scan_cooldown_seconds", 5)

    def run(self):
        self._sync_children()

        while not self._stop.is_set():
            fd = None
            try:
                fd = os.open(str(self._parent), os.O_RDONLY | os.O_DIRECTORY)
                # watch_tree=False (0) — only direct structural changes
                fcntl.ioctl(fd, CIFS_IOC_NOTIFY, pack("=IB", CF_DIR_CHANGES, 0))
                os.close(fd)
                fd = None

                if self._stop.is_set():
                    break
                time.sleep(self._cooldown)
                self._sync_children()

            except OSError as exc:
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                if self._stop.is_set():
                    break
                logger.warning("SubfolderManager %s: %s — retrying in 60s", self._parent.name, exc)
                self._stop.wait(60)

        # Propagate stop to all child watchers
        for _watcher, child_stop in self._child_watchers.values():
            child_stop.set()

    def _sync_children(self):
        try:
            current = {
                d.name for d in self._parent.iterdir()
                if d.is_dir() and not d.name.startswith(".")
            }
        except OSError:
            return

        # Filter excluded
        rel_prefix = self._parent.name
        current = {
            d for d in current
            if f"{rel_prefix}/{d}" not in self._exclude
        }

        # Start watchers for new subdirs
        for name in current - set(self._child_watchers):
            path = self._parent / name
            child_stop = threading.Event()
            watcher = FolderWatcher(path, self._config, child_stop, self._db_lock)
            watcher.start()
            self._child_watchers[name] = (watcher, child_stop)
            logger.info("Started watcher: %s/%s", rel_prefix, name)

        # Stop and remove watchers for deleted subdirs
        for name in set(self._child_watchers) - current:
            _watcher, child_stop = self._child_watchers.pop(name)
            child_stop.set()
            logger.info("Stopped watcher for removed subfolder: %s/%s", rel_prefix, name)


class MountMonitor(threading.Thread):
    """Polls mount every 60s. Triggers CacheRebuilder on remount."""

    daemon = True

    def __init__(
        self, server_root: Path, cache_rebuilder: "CacheRebuilder",
        stop_event: threading.Event,
    ):
        super().__init__(name="MountMonitor")
        self._root = server_root
        self._rebuilder = cache_rebuilder
        self._stop = stop_event
        self._was_mounted = self._check_mount()

    def run(self):
        while not self._stop.wait(60):
            is_mounted = self._check_mount()
            if not self._was_mounted and is_mounted:
                logger.warning("Mount restored at %s — triggering cache rebuild", self._root)
                self._rebuilder.trigger_now()
            elif self._was_mounted and not is_mounted:
                logger.warning("Mount lost at %s", self._root)
            self._was_mounted = is_mounted

    def _check_mount(self) -> bool:
        try:
            return os.path.ismount(str(self._root))
        except OSError:
            return False


class StabilityChecker(threading.Thread):
    """Every 10 minutes: re-stat queued files and mark stable ones."""

    daemon = True

    def __init__(self, config: dict, stop_event: threading.Event, db_lock: threading.Lock):
        super().__init__(name="StabilityChecker")
        self._config = config
        self._stop = stop_event
        self._db_lock = db_lock
        self._interval = 600  # 10 min

    def run(self):
        while not self._stop.wait(self._interval):
            with self._db_lock:
                conn = _get_conn(self._config)
                marked = mark_stable_files(conn, self._config["stationary_seconds"])
                conn.close()
            if marked > 0:
                logger.info("StabilityChecker: marked %d files as stable", marked)


class CacheRebuilder(threading.Thread):
    """Incremental safety-net rebuild. Runs every 24h or on MountMonitor trigger."""

    daemon = True

    def __init__(
        self, watched_folders: list[Path], config: dict,
        stop_event: threading.Event, db_lock: threading.Lock,
    ):
        super().__init__(name="CacheRebuilder")
        self._folders = watched_folders
        self._config = config
        self._stop = stop_event
        self._db_lock = db_lock
        self._trigger = threading.Event()
        self._interval = config.get("full_rebuild_interval_hours", 24) * 3600
        self._exts = set(config["supported_extensions"]["docs"] + config["supported_extensions"]["dbs"])

    def trigger_now(self):
        self._trigger.set()

    def run(self):
        while not self._stop.is_set():
            self._trigger.wait(timeout=self._interval)
            self._trigger.clear()
            if self._stop.is_set():
                break
            self._rebuild_incremental()

    def _rebuild_incremental(self):
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()

        with self._db_lock:
            conn = _get_conn(self._config)

            for folder in self._folders:
                if self._stop.is_set():
                    break

                # Skip if rebuilt recently
                row = conn.execute(
                    "SELECT completed_at FROM rebuild_progress WHERE folder = ?",
                    (str(folder),),
                ).fetchone()
                if row and row[0] > cutoff:
                    continue

                try:
                    logger.info("CacheRebuilder: scanning %s", folder)
                    current = _targeted_find(folder, self._exts, self._config.get("exclude_path_substrings"))
                    update_cache_and_queue(conn, str(folder), current)
                    conn.execute(
                        "INSERT OR REPLACE INTO rebuild_progress (folder, completed_at) VALUES (?, ?)",
                        (str(folder), datetime.now(timezone.utc).isoformat()),
                    )
                    conn.commit()
                    logger.info("CacheRebuilder: done %s (%d files)", folder.name, len(current))
                except OSError as exc:
                    logger.warning("CacheRebuilder: failed on %s: %s", folder.name, exc)

            conn.close()


# ---------------------------------------------------------------------------
# SharePoint poller
# ---------------------------------------------------------------------------


class SharePointPoller(threading.Thread):
    """Polls SharePoint document libraries for changes via Graph delta API.

    Runs every `poll_interval_minutes` (default 30). Falls back to full sync
    automatically if no delta baseline exists for a drive.

    Only started if agent.ingest.sharepoint_sync is importable and
    SHAREPOINT_CONFIG is set / the default path exists.
    """

    daemon = True

    def __init__(self, stop_event: threading.Event):
        super().__init__(name="SharePointPoller")
        self._stop = stop_event

    def run(self):
        if not _SP_AVAILABLE:
            logger.warning("SharePointPoller: sharepoint_sync not available — skipping")
            return

        try:
            cfg = _sp_load_config()
        except FileNotFoundError:
            logger.info("SharePointPoller: no sharepoint.yaml — skipping")
            return
        except Exception as exc:
            logger.error("SharePointPoller: failed to load config: %s", exc)
            return

        interval = cfg.get("poll_interval_minutes", 30) * 60

        while not self._stop.is_set():
            try:
                token = _sp_authenticate(cfg["auth"])
            except Exception as exc:
                logger.error("SharePointPoller: auth failed: %s", exc)
                self._stop.wait(interval)
                continue

            for site in cfg.get("sites", []):
                if self._stop.is_set():
                    break
                try:
                    stats = _sp_delta_sync(site, cfg, token)
                    if any(v > 0 for v in stats.values() if isinstance(v, int)):
                        logger.info(
                            "SharePointPoller: %s — %s", site["name"], stats
                        )
                except Exception as exc:
                    logger.error(
                        "SharePointPoller: delta sync failed for %s: %s",
                        site["name"], exc,
                    )

            self._stop.wait(interval)


# ---------------------------------------------------------------------------
# Main daemon
# ---------------------------------------------------------------------------


def _build_watch_list(config: dict) -> list[Path]:
    """Build the full list of leaf folders the CacheRebuilder should cover."""
    root = Path(config["server_root"])
    exclude = set(config.get("exclude_subfolders", []))
    folders: list[Path] = []

    for name in config.get("watch_toplevel", []):
        folders.append(root / name)

    for parent_name in config.get("watch_subfolder_level", []):
        parent = root / parent_name
        if not parent.exists():
            continue
        for child in sorted(parent.iterdir()):
            if child.is_dir() and not child.name.startswith("."):
                rel = f"{parent_name}/{child.name}"
                if rel not in exclude:
                    folders.append(child)

    return folders


def run_daemon(config: dict) -> None:
    root = Path(config["server_root"])
    exclude = set(config.get("exclude_subfolders", []))
    stop_event = threading.Event()
    db_lock = threading.Lock()

    # Signal handling
    def _shutdown(signum, frame):
        logger.info("Received signal %d — shutting down", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    # Build folder list for CacheRebuilder
    all_leaf_folders = _build_watch_list(config)

    # Start CacheRebuilder first (needed by MountMonitor)
    rebuilder = CacheRebuilder(all_leaf_folders, config, stop_event, db_lock)
    rebuilder.start()

    # MountMonitor
    monitor = MountMonitor(root, rebuilder, stop_event)
    monitor.start()

    # StabilityChecker
    checker = StabilityChecker(config, stop_event, db_lock)
    checker.start()

    # Top-level folder watchers
    for name in config.get("watch_toplevel", []):
        folder = root / name
        if folder.exists():
            w = FolderWatcher(folder, config, stop_event, db_lock)
            w.start()

    # SubfolderManagers for watch_subfolder_level
    for parent_name in config.get("watch_subfolder_level", []):
        parent = root / parent_name
        if parent.exists():
            mgr = SubfolderManager(parent, exclude, config, stop_event, db_lock)
            mgr.start()

    # SharePoint poller (no-op if sharepoint.yaml absent or msal not installed)
    sp_poller = SharePointPoller(stop_event)
    sp_poller.start()

    logger.info("Watcher daemon started — watching %d leaf folders", len(all_leaf_folders))

    # Block main thread until stop
    while not stop_event.is_set():
        stop_event.wait(1)

    logger.info("Watcher daemon stopped")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(threadName)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    parser = argparse.ArgumentParser(description="QNOE SMB3 file watcher daemon")
    parser.add_argument("--config", default=None, help="Path to watcher.yaml")
    parser.add_argument("--rebuild-cache", action="store_true", help="One-shot full cache rebuild")
    parser.add_argument("--dump-queue", action="store_true", help="Print pending change queue")
    args = parser.parse_args()

    config = _load_config(args.config)

    if args.rebuild_cache:
        db_lock = threading.Lock()
        stop = threading.Event()
        folders = _build_watch_list(config)
        rebuilder = CacheRebuilder(folders, config, stop, db_lock)
        logger.info("Running one-shot cache rebuild on %d folders...", len(folders))
        rebuilder._rebuild_incremental()
        logger.info("Done.")
        return

    if args.dump_queue:
        conn = _get_conn(config)
        pending = get_pending_queue(conn)
        if not pending:
            print("No pending entries.")
        else:
            print(f"{'ID':>6}  {'Type':>8}  {'Ext':>6}  {'Stable':>6}  Path")
            print("-" * 80)
            for e in pending:
                stable = "yes" if e["stable_at"] else "no"
                print(f"{e['id']:>6}  {e['change_type']:>8}  {e['ext']:>6}  {stable:>6}  {e['file_path']}")
        conn.close()
        return

    run_daemon(config)


if __name__ == "__main__":
    main()
