"""The three-stage comparison pipeline: size bucket -> partial hash -> full hash.

Hashing everything is prohibitively expensive, so this pipeline discards
candidates as cheaply as possible and only reads bytes when it must:

1. Size buckets. Two files of different sizes can never be equal. If an A-side
   size is unique within A and absent from B, that file is exclusive and
   unique -- decided with zero bytes read.
2. Partial hash (first 64 KiB) on the size buckets that remain.
3. Sampled hash (middle + last 64 KiB + size) for files above 8 MiB that
   survive stage 2. Purely eliminating: it can prove two files different, but
   a match always falls through to stage 4. See "Hashing Strategy Evaluation"
   in docs/superpowers/plans/2026-08-04-resumability.md for why identity is
   never concluded from a sample.
4. Full SHA-256 only on files that survive stage 3.

On typical photo/document trees this avoids reading well over 90% of the bytes.

`io_workers` parallelizes only the hashing calls in stages 2 and 3 via a
ThreadPoolExecutor; bucketing, representative selection and classification
stay sequential. At the default io_workers=1 the hashing calls run in the
exact same order as a plain loop, so behavior is unchanged from before this
was added -- see tests.test_comparer.IoWorkersEquivalenceTests.
"""

import time
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from dupefinder.hashing import SAMPLE_THRESHOLD, HashCache, ReadError
from dupefinder.models import FileEntry, Report, ReportRow, ScanError, ScanProgress, Stats


def compare(
    entries_a: list[FileEntry],
    entries_b: list[FileEntry],
    cache: HashCache,
    progress: Callable[[ScanProgress], None] | None = None,
    io_workers: int = 1,
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

    all_sizes = sorted(by_size_a.keys())
    total = len(all_sizes)

    # bytes_to_resolve is an upper bound, computed once: the total size of
    # every file that actually enters the slow path. Overestimating is the
    # right bias -- the ETA shrinks as work proceeds instead of blowing past
    # a promised finish time.
    bytes_to_resolve = 0
    for size in all_sizes:
        group_a = by_size_a[size]
        if len(group_a) == 1 and size not in by_size_b:
            continue
        bytes_to_resolve += sum(e.size for e in group_a)
        bytes_to_resolve += sum(e.size for e in by_size_b.get(size, []))

    b_full_hashes: set[str] = set()
    a_representative: dict[str, str] = {}
    bytes_resolved = 0
    current_path = ""

    executor = ThreadPoolExecutor(max_workers=io_workers) if io_workers > 1 else None
    try:
        for processed, size in enumerate(all_sizes, start=1):
            if progress is not None:
                progress(
                    ScanProgress(
                        buckets_done=processed,
                        buckets_total=total,
                        bytes_resolved=bytes_resolved,
                        bytes_to_resolve=bytes_to_resolve,
                        current_path=current_path,
                    )
                )

            group_a = by_size_a[size]
            group_b = by_size_b.get(size, [])

            # FAST PATH: a size unique across A and absent from B needs no hashing.
            if len(group_a) == 1 and size not in by_size_b:
                entry = group_a[0]
                rows.append(_row(entry, status="exclusive"))
                continue

            # SLOW PATH, stage 2: bucket by partial (64 KiB) hash.
            partial_buckets: dict[str, list[tuple[FileEntry, str]]] = defaultdict(list)
            slow_entries = [(e, "a") for e in group_a] + [(e, "b") for e in group_b]

            for entry, origin, result in _hash_many(slow_entries, cache.partial, executor):
                current_path = entry.abs_path
                if isinstance(result, ReadError):
                    if origin == "a":
                        rows.append(_row(entry, status="unreadable"))
                    errors.append(ScanError(path=entry.abs_path, error=str(result)))
                    bytes_resolved += entry.size
                    continue
                partial_buckets[result].append((entry, origin))

            # SLOW PATH, stage 3: refine the partial buckets with a sampled
            # hash before paying for any full read.
            b_digests_this_size: set[str] = set()
            a_with_full_hash: list[tuple[FileEntry, str]] = []
            refined_groups: list[list[tuple[FileEntry, str]]] = []

            for group in partial_buckets.values():
                if len(group) == 1 and group[0][1] == "a":
                    entry, _origin = group[0]
                    rows.append(_row(entry, status="exclusive"))
                    bytes_resolved += entry.size
                    continue

                # Only worth two extra seeks on files big enough that a full
                # read would dominate. Below the threshold, go straight to the
                # full hash exactly as before.
                if not all(e.size >= SAMPLE_THRESHOLD for e, _o in group):
                    refined_groups.append(group)
                    continue

                sampled_buckets: dict[str, list[tuple[FileEntry, str]]] = defaultdict(list)
                for entry, origin, result in _hash_many(group, cache.sampled, executor):
                    current_path = entry.abs_path
                    if isinstance(result, ReadError):
                        if origin == "a":
                            rows.append(_row(entry, status="unreadable"))
                        errors.append(ScanError(path=entry.abs_path, error=str(result)))
                        bytes_resolved += entry.size
                        continue
                    sampled_buckets[result].append((entry, origin))

                for subgroup in sampled_buckets.values():
                    # A file alone in its sampled bucket cannot be byte
                    # identical to anything else here: identical content
                    # necessarily produces an identical sampled hash.
                    if len(subgroup) == 1 and subgroup[0][1] == "a":
                        entry, _origin = subgroup[0]
                        rows.append(_row(entry, status="exclusive"))
                        bytes_resolved += entry.size
                        continue
                    # A bucket with no A member can never be consulted: only
                    # A files are classified against b_full_hashes, and a
                    # matching A file would share this sampled hash and so
                    # would be in this very bucket.
                    if not any(origin == "a" for _e, origin in subgroup):
                        for entry, _origin in subgroup:
                            bytes_resolved += entry.size
                        continue
                    refined_groups.append(subgroup)

            # A sampled match proves nothing on its own -- the full hash is
            # what decides identity.
            for group in refined_groups:
                for entry, origin, result in _hash_many(group, cache.full, executor):
                    current_path = entry.abs_path
                    if isinstance(result, ReadError):
                        if origin == "a":
                            rows.append(_row(entry, status="unreadable"))
                        errors.append(ScanError(path=entry.abs_path, error=str(result)))
                        bytes_resolved += entry.size
                        continue
                    if origin == "b":
                        b_digests_this_size.add(result)
                    else:
                        a_with_full_hash.append((entry, result))
                    bytes_resolved += entry.size

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
                            entry, status="internal_copy", sha256=digest,
                            duplicate_of=a_representative[digest],
                        )
                    )

        # Final emission. The loop above reports at the TOP of each bucket, so
        # without this the last bucket's work is never reported: the UI would
        # stall short of 100% and a single-bucket scan would report zero
        # progress for its entire duration. Do not remove this.
        if progress is not None:
            progress(
                ScanProgress(
                    buckets_done=total,
                    buckets_total=total,
                    bytes_resolved=bytes_resolved,
                    bytes_to_resolve=bytes_to_resolve,
                    current_path="",
                )
            )
    finally:
        if executor is not None:
            executor.shutdown(wait=True)

    rows.sort(key=lambda r: r.rel_path)

    stats = Stats(
        files_a=len(entries_a),
        files_b=len(entries_b),
        bytes_a=sum(e.size for e in entries_a),
        bytes_b=sum(e.size for e in entries_b),
        partial_hashes=cache.partial_calls,
        sampled_hashes=cache.sampled_calls,
        full_hashes=cache.full_calls,
        bytes_read=cache.bytes_read,
        elapsed_seconds=time.perf_counter() - start,
    )

    return Report(rows=rows, errors=errors, stats=stats)


def _hash_many(
    items: list[tuple[FileEntry, str]],
    hash_fn: Callable[[str], str],
    executor: ThreadPoolExecutor | None,
):
    """Yield (entry, origin, result) for every (entry, origin) in `items`.

    `result` is either the digest string or the caught ReadError. Sequential
    when `executor` is None -- the exact code path used at io_workers=1, zero
    regression risk. With an executor, `Executor.map` preserves input order in
    its results, so accounting and classification never depend on which
    thread finishes first -- only wall-clock time changes.

    `items` is materialized into a list up front because the threaded branch
    below iterates it twice (once for `executor.map`, once for `zip`).
    `Executor.map` eagerly drains its input, so a lazy iterable would be fully
    consumed by the first pass and `zip` would then see it exhausted, silently
    yielding zero pairs -- every file in the bucket would vanish with no
    exception. Both current call sites already pass concrete lists, so this is
    a structural guard against a future caller passing something lazy, not a
    live bug.
    """
    items = list(items)
    if executor is None:
        for entry, origin in items:
            try:
                yield entry, origin, hash_fn(entry.abs_path)
            except ReadError as exc:
                yield entry, origin, exc
        return

    def _safe_hash(pair):
        entry, _origin = pair
        try:
            return hash_fn(entry.abs_path)
        except ReadError as exc:
            return exc

    for (entry, origin), result in zip(items, executor.map(_safe_hash, items)):
        yield entry, origin, result


def _row(
    entry: FileEntry, status: str, sha256: str | None = None, duplicate_of: str | None = None
) -> ReportRow:
    return ReportRow(
        id=entry.rel_path, abs_path=entry.abs_path, rel_path=entry.rel_path, size=entry.size,
        status=status, sha256=sha256, duplicate_of=duplicate_of,
    )
