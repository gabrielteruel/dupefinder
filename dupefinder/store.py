"""SQLite-backed persistence for file hashes and settings.

Location is a correctness requirement, not a preference: SQLite relies on
POSIX advisory locks, which are unreliable over 9p/drvfs and network mounts,
so the database must never be created on a scanned volume.
"""

import json
import os
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass

SCHEMA_VERSION = 1


@dataclass
class HashRow:
    path: str
    size: int
    mtime_ns: int
    # Defaults let a caller name only the digest it actually computed. Field
    # order must stay in sync with the SELECT in get_hash(), which unpacks
    # positionally.
    partial: str | None = None   # SHA-256 of the first 64 KiB
    sampled: str | None = None   # SHA-256 of middle + last 64 KiB + size (large files only)
    full: str | None = None      # SHA-256 of the whole file


def cache_dir(override: str | None = None) -> str:
    """Resolve the OS-appropriate cache directory, creating it if needed."""
    if override:
        path = override
    elif sys.platform == "darwin":
        path = os.path.join(os.path.expanduser("~"), "Library", "Caches", "dupefinder")
    elif os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        path = os.path.join(base, "dupefinder")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
        path = os.path.join(base, "dupefinder")
    os.makedirs(path, exist_ok=True)
    return path


class Store:
    """SQLite-backed hash cache and settings store.

    Three-level durability model: an in-memory pending buffer is flushed to
    disk every `flush_rows` rows or `flush_seconds`, whichever comes first,
    and always on close(). This bounds crash loss to at most a couple of
    seconds of hashing instead of the whole run.
    """

    def __init__(self, db_path: str, flush_rows: int = 200, flush_seconds: float = 2.0) -> None:
        self._db_path = db_path
        self._flush_rows = flush_rows
        self._flush_seconds = flush_seconds
        self._lock = threading.Lock()
        self._pending: dict[str, HashRow] = {}
        self._last_flush = time.monotonic()
        self._disabled = False
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._init_schema()

    def _init_schema(self) -> None:
        with self._conn:
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = NORMAL")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS file_hashes (
                    path        TEXT PRIMARY KEY,
                    size        INTEGER NOT NULL,
                    mtime_ns    INTEGER NOT NULL,
                    partial     TEXT,
                    sampled     TEXT,
                    full        TEXT,
                    last_seen   INTEGER NOT NULL
                )
                """
            )
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_meta (version INTEGER NOT NULL)"
            )
            row = self._conn.execute("SELECT version FROM schema_meta").fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO schema_meta (version) VALUES (?)", (SCHEMA_VERSION,)
                )
            elif row[0] != SCHEMA_VERSION:
                print(
                    f"dupefinder: cache schema version {row[0]} is not the supported "
                    f"version {SCHEMA_VERSION}; running with the persistent cache disabled",
                    file=sys.stderr,
                )
                self._disabled = True

    def get_hash(self, path: str, size: int, mtime_ns: int) -> HashRow | None:
        """Return the stored row only if size and mtime_ns both match; else None."""
        if self._disabled:
            return None
        with self._lock:
            row = self._pending.get(path)
            if row is None:
                cur = self._conn.execute(
                    "SELECT path, size, mtime_ns, partial, sampled, full "
                    "FROM file_hashes WHERE path = ?",
                    (path,),
                )
                result = cur.fetchone()
                row = HashRow(*result) if result else None
        if row is None or row.size != size or row.mtime_ns != mtime_ns:
            return None
        return row

    def put_hash(self, row: HashRow) -> None:
        """Queue a row for write; flushes automatically at the configured thresholds.

        If a row is already pending for this path AND it's the same file version
        (size and mtime_ns both match), merge sibling digest fields instead of
        overwriting -- two calls computing different digests for the same file
        (e.g. partial() then full()) must not lose each other's work before a
        flush even happens. A genuinely new file version (different size or
        mtime_ns) replaces the pending row outright; its digests belong to
        different bytes and must never be carried forward.
        """
        if self._disabled:
            return
        with self._lock:
            existing = self._pending.get(row.path)
            if existing is not None and existing.size == row.size and existing.mtime_ns == row.mtime_ns:
                row = HashRow(
                    path=row.path,
                    size=row.size,
                    mtime_ns=row.mtime_ns,
                    partial=row.partial if row.partial is not None else existing.partial,
                    sampled=row.sampled if row.sampled is not None else existing.sampled,
                    full=row.full if row.full is not None else existing.full,
                )
            self._pending[row.path] = row
            due = (
                len(self._pending) >= self._flush_rows
                or time.monotonic() - self._last_flush >= self._flush_seconds
            )
        if due:
            self.flush()

    def flush(self) -> None:
        # The whole flush holds the lock. Releasing it before the write would
        # open a window where a row is in neither _pending nor the database,
        # so a concurrent get_hash() for that path would miss and re-hash a
        # file that was already done. Batches are small (flush_rows), so this
        # never blocks a hashing thread for long.
        with self._lock:
            rows = list(self._pending.values())
            self._pending.clear()
            self._last_flush = time.monotonic()
            if not rows:
                return
            now = int(time.time())
            with self._conn:
                self._conn.executemany(
                    "INSERT INTO file_hashes "
                    "(path, size, mtime_ns, partial, sampled, full, last_seen) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(path) DO UPDATE SET "
                    "size = excluded.size, "
                    "mtime_ns = excluded.mtime_ns, "
                    "partial = CASE "
                    "    WHEN file_hashes.size = excluded.size AND file_hashes.mtime_ns = excluded.mtime_ns "
                    "    THEN COALESCE(excluded.partial, file_hashes.partial) "
                    "    ELSE excluded.partial "
                    "END, "
                    "sampled = CASE "
                    "    WHEN file_hashes.size = excluded.size AND file_hashes.mtime_ns = excluded.mtime_ns "
                    "    THEN COALESCE(excluded.sampled, file_hashes.sampled) "
                    "    ELSE excluded.sampled "
                    "END, "
                    "full = CASE "
                    "    WHEN file_hashes.size = excluded.size AND file_hashes.mtime_ns = excluded.mtime_ns "
                    "    THEN COALESCE(excluded.full, file_hashes.full) "
                    "    ELSE excluded.full "
                    "END, "
                    "last_seen = excluded.last_seen",
                    [
                        (r.path, r.size, r.mtime_ns, r.partial, r.sampled, r.full, now)
                        for r in rows
                    ],
                )

    def close(self) -> None:
        self.flush()
        self._conn.close()

    def get_setting(self, key: str) -> object | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def set_setting(self, key: str, value: object) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value)),
            )

    def row_count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM file_hashes").fetchone()[0]

    def db_size_bytes(self) -> int:
        try:
            return os.stat(self._db_path).st_size
        except OSError:
            return 0

    def clear_hashes(self) -> None:
        with self._lock:
            self._pending.clear()
            with self._conn:
                self._conn.execute("DELETE FROM file_hashes")
