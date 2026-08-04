"""Content hashing with a cheap partial-hash pre-filter and a per-job cache."""

import hashlib
import os
import threading

CHUNK_SIZE = 1024 * 1024  # 1 MiB read buffer
PARTIAL_SIZE = 64 * 1024  # 64 KiB prefix used by the cheap pre-filter


class ReadError(Exception):
    """Raised when a file cannot be read. Carries the offending path."""

    def __init__(self, path: str, cause: Exception) -> None:
        super().__init__(f"could not read {path}: {cause}")
        self.path = path
        self.cause = cause


def partial_hash(path: str) -> str:
    """SHA-256 hex digest of the first PARTIAL_SIZE bytes. Raises ReadError."""
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            digest.update(f.read(PARTIAL_SIZE))
    except OSError as exc:
        raise ReadError(path, exc) from exc
    return digest.hexdigest()


def full_hash(path: str) -> str:
    """SHA-256 hex digest of the whole file, read in CHUNK_SIZE blocks.

    Never loads the file into memory at once. Raises ReadError.
    """
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while chunk := f.read(CHUNK_SIZE):
                digest.update(chunk)
    except OSError as exc:
        raise ReadError(path, exc) from exc
    return digest.hexdigest()


class HashCache:
    """Memoizes digests per absolute path for the lifetime of one scan job.

    Also accumulates the statistics reported to the UI. Thread-safe: a lock
    guards only dict and counter mutation, never open()/read() itself, so
    concurrent reads genuinely overlap.
    """

    def __init__(self) -> None:
        self._partial: dict[str, str] = {}
        self._full: dict[str, str] = {}
        self._lock = threading.Lock()
        self.partial_calls = 0
        self.full_calls = 0
        self.bytes_read = 0

    def partial(self, path: str) -> str:
        with self._lock:
            cached = self._partial.get(path)
        if cached is not None:
            return cached

        digest = partial_hash(path)  # I/O outside the lock -- this is the point
        try:
            size = os.stat(path).st_size
        except OSError:
            size = 0

        with self._lock:
            self._partial[path] = digest
            self.partial_calls += 1
            self.bytes_read += min(size, PARTIAL_SIZE)
        return digest

    def full(self, path: str) -> str:
        with self._lock:
            cached = self._full.get(path)
        if cached is not None:
            return cached

        digest = full_hash(path)
        try:
            size = os.stat(path).st_size
        except OSError:
            size = 0

        with self._lock:
            self._full[path] = digest
            self.full_calls += 1
            self.bytes_read += size
        return digest

    def close(self) -> None:
        """No-op for the in-memory cache; overridden by PersistentHashCache."""
