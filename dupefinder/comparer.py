"""The three-stage comparison pipeline: size bucket -> partial hash -> full hash.

Hashing everything is prohibitively expensive, so this pipeline discards
candidates as cheaply as possible and only reads bytes when it must:

1. Size buckets. Two files of different sizes can never be equal. If an A-side
   size is unique within A and absent from B, that file is exclusive and
   unique -- decided with zero bytes read.
2. Partial hash (first 64 KiB) on the size buckets that remain.
3. Full SHA-256 only on files that survive stage 2.

On typical photo/document trees this avoids reading well over 90% of the bytes.
"""

import time
from collections import defaultdict
from collections.abc import Callable

from dupefinder.hashing import HashCache, ReadError
from dupefinder.models import FileEntry, Report, ReportRow, ScanError, Stats


def compare(
    entries_a: list[FileEntry],
    entries_b: list[FileEntry],
    cache: HashCache,
    progress: Callable[[int, int], None] | None = None,
) -> Report:
    start = time.perf_counter()
    rows: list[ReportRow] = []
    errors: list[ScanError] = []

    by_size_a: dict[int, list[FileEntry]] = defaultdict(list)
    for entry in entries_a:
        by_size_a[entry.size].append(entry)
    by_size_b: dict[int, list[FileEntry]] = defaultdict(list)
    for entry in entries_b:
        by_size_b[entry.size].append(entry)
    for group in by_size_a.values():
        group.sort(key=lambda e: e.rel_path)
    for group in by_size_b.values():
        group.sort(key=lambda e: e.rel_path)

    b_full_hashes: set[str] = set()
    a_representative: dict[str, str] = {}

    all_sizes = sorted(by_size_a.keys())
    total = len(all_sizes)

    for processed, size in enumerate(all_sizes, start=1):
        if progress is not None:
            progress(processed, total)

        group_a = by_size_a[size]
        group_b = by_size_b.get(size, [])

        # FAST PATH: a size unique across A and absent from B needs no hashing.
        if len(group_a) == 1 and size not in by_size_b:
            entry = group_a[0]
            rows.append(_row(entry, status="exclusive"))
            continue

        # SLOW PATH, stage 2: bucket by partial (64 KiB) hash.
        partial_buckets: dict[str, list[tuple[FileEntry, str]]] = defaultdict(list)
        for entry in group_a:
            try:
                digest = cache.partial(entry.abs_path)
            except ReadError as exc:
                rows.append(_row(entry, status="unreadable"))
                errors.append(ScanError(path=entry.abs_path, error=str(exc)))
                continue
            partial_buckets[digest].append((entry, "a"))
        for entry in group_b:
            try:
                digest = cache.partial(entry.abs_path)
            except ReadError as exc:
                errors.append(ScanError(path=entry.abs_path, error=str(exc)))
                continue
            partial_buckets[digest].append((entry, "b"))

        # SLOW PATH, stage 3: full hash, only where the partial hash did not
        # already prove a file unique to A.
        b_digests_this_size: set[str] = set()
        a_with_full_hash: list[tuple[FileEntry, str]] = []

        for group in partial_buckets.values():
            if len(group) == 1 and group[0][1] == "a":
                entry, _origin = group[0]
                rows.append(_row(entry, status="exclusive"))
                continue

            for entry, origin in group:
                try:
                    digest = cache.full(entry.abs_path)
                except ReadError as exc:
                    if origin == "a":
                        rows.append(_row(entry, status="unreadable"))
                    errors.append(ScanError(path=entry.abs_path, error=str(exc)))
                    continue
                if origin == "b":
                    b_digests_this_size.add(digest)
                else:
                    a_with_full_hash.append((entry, digest))

        b_full_hashes |= b_digests_this_size

        a_with_full_hash.sort(key=lambda pair: pair[0].rel_path)
        for entry, digest in a_with_full_hash:
            if digest in b_full_hashes:
                rows.append(_row(entry, status="present_in_b", sha256=digest))
            elif digest not in a_representative:
                a_representative[digest] = entry.rel_path
                rows.append(_row(entry, status="exclusive", sha256=digest))
            else:
                rows.append(
                    _row(
                        entry,
                        status="internal_copy",
                        sha256=digest,
                        duplicate_of=a_representative[digest],
                    )
                )

    rows.sort(key=lambda r: r.rel_path)

    stats = Stats(
        files_a=len(entries_a),
        files_b=len(entries_b),
        bytes_a=sum(e.size for e in entries_a),
        bytes_b=sum(e.size for e in entries_b),
        partial_hashes=cache.partial_calls,
        full_hashes=cache.full_calls,
        bytes_read=cache.bytes_read,
        elapsed_seconds=time.perf_counter() - start,
    )

    return Report(rows=rows, errors=errors, stats=stats)


def _row(
    entry: FileEntry,
    status: str,
    sha256: str | None = None,
    duplicate_of: str | None = None,
) -> ReportRow:
    return ReportRow(
        id=entry.rel_path,
        abs_path=entry.abs_path,
        rel_path=entry.rel_path,
        size=entry.size,
        status=status,
        sha256=sha256,
        duplicate_of=duplicate_of,
    )
