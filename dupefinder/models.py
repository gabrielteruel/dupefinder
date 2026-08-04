"""Shared data structures used across the scanner, comparer, mover and server."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class FileEntry:
    """A single file discovered during a scan."""

    abs_path: str  # absolute, realpath-resolved
    rel_path: str  # relative to its scan root, always POSIX "/" separators
    size: int  # bytes


@dataclass
class ScanError:
    """A file or directory that could not be read during a scan."""

    path: str
    error: str  # str(exception)


@dataclass
class NoisyDir:
    """A directory the pre-scan flags as likely irrelevant (VCS, caches, etc.)."""

    abs_path: str
    rel_path: str
    root: str  # "A" or "B"
    reason: str  # "vcs" | "dependencies" | "cache" | "system" | "hidden"
    file_count: int
    total_bytes: int
    counts_truncated: bool = False  # True when the walk hit its limit; counts are a floor


@dataclass
class ReportRow:
    """One classified file from folder A, as shown in the UI report table."""

    id: str  # equals rel_path; stable identifier used by the UI
    abs_path: str
    rel_path: str
    size: int
    status: str  # "exclusive" | "internal_copy" | "present_in_b" | "unreadable"
    sha256: str | None = None  # None when the file was never hashed
    duplicate_of: str | None = None  # rel_path of the representative; only for internal_copy


@dataclass
class Stats:
    """Aggregate counters describing a comparison run."""

    files_a: int = 0
    files_b: int = 0
    bytes_a: int = 0
    bytes_b: int = 0
    partial_hashes: int = 0
    full_hashes: int = 0
    bytes_read: int = 0
    elapsed_seconds: float = 0.0


@dataclass
class Report:
    """The full result of comparing folder A against folder B."""

    rows: list[ReportRow] = field(default_factory=list)
    errors: list[ScanError] = field(default_factory=list)
    stats: Stats = field(default_factory=Stats)


@dataclass
class ScanProgress:
    """A snapshot of comparison progress, driven by bytes rather than buckets.

    A bucket holding three photos and one holding three 2 GB videos count the
    same as bucket counts but differ by orders of magnitude in cost, so the
    UI's ETA is driven by bytes_resolved / bytes_to_resolve instead.
    """

    buckets_done: int
    buckets_total: int
    bytes_resolved: int      # bytes whose hash is now known (disk or cache)
    bytes_to_resolve: int    # upper bound, computed once after bucketing
    current_path: str        # for the UI, so a slow scan doesn't look frozen
