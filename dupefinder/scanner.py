"""Directory walking: file discovery and noisy-directory detection."""

import os
import time
from collections.abc import Callable

from dupefinder.models import FileEntry, NoisyDir, ScanError

NOISY_PATTERNS: dict[str, str] = {
    ".git": "vcs",
    ".svn": "vcs",
    ".hg": "vcs",
    "node_modules": "dependencies",
    "vendor": "dependencies",
    ".venv": "dependencies",
    "venv": "dependencies",
    "__pycache__": "cache",
    ".cache": "cache",
    ".pytest_cache": "cache",
    ".mypy_cache": "cache",
    "$RECYCLE.BIN": "system",
    "System Volume Information": "system",
    ".Trash-1000": "system",
}

# Upper bound on the informational file count shown for a noisy directory.
COUNT_LIMIT = 2000


def find_noisy_dirs(root: str, root_label: str) -> list[NoisyDir]:
    """Find directories that are likely irrelevant to the comparison.

    Top-down walk. When a directory name matches NOISY_PATTERNS, or starts with
    '.' and is not the root itself, record it and prune it (do not descend) so
    that, e.g., a .git nested inside an already-reported node_modules is not
    reported a second time. Symlinked directories are skipped entirely and
    never reported.
    """
    noisy: list[NoisyDir] = []

    for dirpath, dirnames, _filenames in os.walk(root, followlinks=False):
        keep: list[str] = []
        for dirname in dirnames:
            abs_path = os.path.join(dirpath, dirname)

            if os.path.islink(abs_path):
                continue  # never reported, never descended into

            reason = NOISY_PATTERNS.get(dirname)
            if reason is None and dirname.startswith("."):
                reason = "hidden"

            if reason is not None:
                file_count, total_bytes, truncated = _count_recursive(abs_path, COUNT_LIMIT)
                rel_path = os.path.relpath(abs_path, root).replace(os.sep, "/")
                noisy.append(
                    NoisyDir(
                        abs_path=abs_path,
                        rel_path=rel_path,
                        root=root_label,
                        reason=reason,
                        file_count=file_count,
                        total_bytes=total_bytes,
                        counts_truncated=truncated,
                    )
                )
                continue  # pruned: not added to `keep`, so os.walk won't descend

            keep.append(dirname)

        dirnames[:] = keep

    return noisy


def _count_recursive(root: str, limit: int = COUNT_LIMIT) -> tuple[int, int, bool]:
    """Count files and total bytes under `root`, not following symlinks.

    Stops once `limit` files have been counted and reports that via the third
    return value. These counts only help the user decide compare/skip/trash, so
    an exact number is not worth an unbounded walk -- external drives routinely
    carry a huge $RECYCLE.BIN, and over a slow filesystem such as WSL's 9p mount
    that walk would stall the pre-scan screen.

    Stat failures are silently skipped since this is informational, not part of
    the comparison.
    """
    file_count = 0
    total_bytes = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
        for filename in filenames:
            if file_count >= limit:
                return file_count, total_bytes, True
            file_path = os.path.join(dirpath, filename)
            if os.path.islink(file_path):
                continue
            try:
                size = os.stat(file_path).st_size
            except OSError:
                continue
            file_count += 1
            total_bytes += size
    return file_count, total_bytes, False


def scan(
    root: str,
    skip_abs_paths: set[str],
    progress: Callable[[int, str], None] | None = None,
) -> tuple[list[FileEntry], list[ScanError]]:
    """Walk `root` and return every file as a FileEntry, sorted by rel_path.

    Directories whose absolute path is in `skip_abs_paths` are pruned. Symlinks
    (files or directories) are never followed or reported. A file that fails
    os.stat() produces a ScanError and is omitted from the returned entries.

    `progress`, if given, is called as `progress(files_found, current_path)`,
    throttled to at most once every 0.2s plus a guaranteed final call, so a
    caller can show live discovery progress without a Python call per file on
    a million-file tree.
    """
    entries: list[FileEntry] = []
    errors: list[ScanError] = []
    last_progress_at = time.monotonic()

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [
            d
            for d in dirnames
            if os.path.join(dirpath, d) not in skip_abs_paths
            and not os.path.islink(os.path.join(dirpath, d))
        ]

        for filename in filenames:
            abs_path = os.path.join(dirpath, filename)
            if os.path.islink(abs_path):
                continue
            try:
                size = os.stat(abs_path).st_size
            except OSError as exc:
                errors.append(ScanError(path=abs_path, error=str(exc)))
                continue
            rel_path = os.path.relpath(abs_path, root).replace(os.sep, "/")
            entries.append(FileEntry(abs_path=abs_path, rel_path=rel_path, size=size))

            if progress is not None:
                now = time.monotonic()
                if now - last_progress_at >= 0.2:
                    progress(len(entries), abs_path)
                    last_progress_at = now

    entries.sort(key=lambda e: e.rel_path)
    if progress is not None:
        progress(len(entries), "")
    return entries, errors
