"""Tests for dupefinder.comparer."""

import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from dupefinder.comparer import _hash_many, compare
from dupefinder.hashing import HashCache, PersistentHashCache
from dupefinder.models import FileEntry, ScanProgress
from dupefinder.store import Store
from tests.helpers import TempTreeCase, build_tree


def _entries(root: str, rel_paths: list[str]) -> list[FileEntry]:
    entries = []
    for rel_path in rel_paths:
        abs_path = os.path.join(root, *rel_path.split("/"))
        entries.append(
            FileEntry(abs_path=abs_path, rel_path=rel_path, size=os.stat(abs_path).st_size)
        )
    return entries


def _rows_by_path(report):
    return {row.rel_path: row for row in report.rows}


class ComparerTests(TempTreeCase):
    def test_same_content_different_filename_is_present_in_b(self) -> None:
        build_tree(
            self.root,
            {
                "a/old_report.pdf": b"same content",
                "b/new_report.pdf": b"same content",
            },
        )
        entries_a = _entries(self.root, ["a/old_report.pdf"])
        entries_b = _entries(self.root, ["b/new_report.pdf"])

        report = compare(entries_a, entries_b, HashCache())

        rows = _rows_by_path(report)
        self.assertEqual(rows["a/old_report.pdf"].status, "present_in_b")

    def test_same_filename_different_content_is_exclusive(self) -> None:
        build_tree(
            self.root,
            {
                "a/same-name.txt": b"P content here",
                "b/same-name.txt": b"Q different content",
            },
        )
        entries_a = _entries(self.root, ["a/same-name.txt"])
        entries_b = _entries(self.root, ["b/same-name.txt"])

        report = compare(entries_a, entries_b, HashCache())

        rows = _rows_by_path(report)
        self.assertEqual(rows["a/same-name.txt"].status, "exclusive")

    def test_same_size_different_content_is_exclusive(self) -> None:
        # Guards against a false positive from the partial-hash stage: both
        # files are the same size but differ, so they must not be conflated.
        build_tree(
            self.root,
            {
                "a/file.bin": b"AAAA",
                "b/file.bin": b"BBBB",
            },
        )
        entries_a = _entries(self.root, ["a/file.bin"])
        entries_b = _entries(self.root, ["b/file.bin"])

        report = compare(entries_a, entries_b, HashCache())

        rows = _rows_by_path(report)
        self.assertEqual(rows["a/file.bin"].status, "exclusive")

    def test_several_empty_files_across_a_and_b_classified_correctly(self) -> None:
        build_tree(
            self.root,
            {
                "a/empty1.txt": b"",
                "a/empty2.txt": b"",
                "b/empty3.txt": b"",
            },
        )
        entries_a = _entries(self.root, ["a/empty1.txt", "a/empty2.txt"])
        entries_b = _entries(self.root, ["b/empty3.txt"])

        report = compare(entries_a, entries_b, HashCache())

        rows = _rows_by_path(report)
        # Both A empty files match B's empty file by content.
        statuses = {rows["a/empty1.txt"].status, rows["a/empty2.txt"].status}
        self.assertEqual(statuses, {"present_in_b"})

    def test_three_identical_a_files_absent_from_b_dedupes_to_one_exclusive(self) -> None:
        build_tree(
            self.root,
            {
                "a/copy1.bin": b"identical content",
                "a/nested/copy2.bin": b"identical content",
                "a/z_copy3.bin": b"identical content",
            },
        )
        entries_a = _entries(
            self.root, ["a/copy1.bin", "a/nested/copy2.bin", "a/z_copy3.bin"]
        )

        report = compare(entries_a, [], HashCache())

        rows = _rows_by_path(report)
        exclusive = [r for r in rows.values() if r.status == "exclusive"]
        internal_copies = [r for r in rows.values() if r.status == "internal_copy"]

        self.assertEqual(len(exclusive), 1)
        self.assertEqual(len(internal_copies), 2)
        # Lexicographically smallest rel_path is the representative.
        self.assertEqual(exclusive[0].rel_path, "a/copy1.bin")
        for row in internal_copies:
            self.assertEqual(row.duplicate_of, "a/copy1.bin")

    def test_unique_size_takes_the_fast_path_without_hashing(self) -> None:
        build_tree(
            self.root,
            {
                "a/unique.bin": b"a size nothing else shares",
                "b/other.bin": b"totally different length!!",
            },
        )
        entries_a = _entries(self.root, ["a/unique.bin"])
        entries_b = _entries(self.root, ["b/other.bin"])
        # Force distinct sizes explicitly, independent of the fixture content.
        entries_a = [FileEntry(entries_a[0].abs_path, entries_a[0].rel_path, size=999)]
        entries_b = [FileEntry(entries_b[0].abs_path, entries_b[0].rel_path, size=111)]

        cache = HashCache()
        report = compare(entries_a, entries_b, cache)

        rows = _rows_by_path(report)
        row = rows["a/unique.bin"]
        self.assertEqual(row.status, "exclusive")
        self.assertIsNone(row.sha256)
        self.assertEqual(cache.full_calls, 0)
        self.assertEqual(cache.partial_calls, 0)

    def test_unreadable_a_file_reported_and_scan_continues(self) -> None:
        build_tree(self.root, {"a/readable.bin": b"12345"})
        os.makedirs(os.path.join(self.root, "a", "unreadable_dir"))
        # A directory masquerading as a file entry always raises ReadError,
        # regardless of the user's privilege level.
        unreadable_path = os.path.join(self.root, "a", "unreadable_dir")

        entries_a = [
            FileEntry(
                abs_path=os.path.join(self.root, "a", "readable.bin"),
                rel_path="a/readable.bin",
                size=5,
            ),
            FileEntry(abs_path=unreadable_path, rel_path="a/unreadable_dir", size=5),
        ]

        report = compare(entries_a, [], HashCache())

        rows = _rows_by_path(report)
        self.assertEqual(rows["a/unreadable_dir"].status, "unreadable")
        self.assertEqual(len(report.errors), 1)
        self.assertEqual(report.errors[0].path, unreadable_path)
        # The other file is still classified normally.
        self.assertIn(rows["a/readable.bin"].status, {"exclusive", "internal_copy"})


class IoWorkersEquivalenceTests(TempTreeCase):
    def _build_scenario_tree(self):
        build_tree(
            self.root,
            {
                "a/old_report.pdf": b"same content",
                "b/new_report.pdf": b"same content",
                "a/same-name.txt": b"P content here",
                "b/same-name.txt": b"Q different content",
                "a/file.bin": b"AAAA",
                "b/file.bin": b"BBBB",
                "a/empty1.txt": b"",
                "a/empty2.txt": b"",
                "b/empty3.txt": b"",
                "a/copy1.bin": b"identical content",
                "a/nested/copy2.bin": b"identical content",
                "a/z_copy3.bin": b"identical content",
                "a/unique.bin": b"nothing else shares this size!",
            },
        )
        entries_a = _entries(
            self.root,
            [
                "a/old_report.pdf", "a/same-name.txt", "a/file.bin", "a/empty1.txt",
                "a/empty2.txt", "a/copy1.bin", "a/nested/copy2.bin", "a/z_copy3.bin", "a/unique.bin",
            ],
        )
        entries_b = _entries(
            self.root, ["b/new_report.pdf", "b/same-name.txt", "b/file.bin", "b/empty3.txt"]
        )
        return entries_a, entries_b

    def _signature(self, report):
        return [
            (r.rel_path, r.status, r.sha256, r.duplicate_of)
            for r in sorted(report.rows, key=lambda r: r.rel_path)
        ]

    def test_io_workers_four_matches_io_workers_one_on_every_scenario(self) -> None:
        entries_a, entries_b = self._build_scenario_tree()

        sequential = compare(entries_a, entries_b, HashCache(), io_workers=1)
        parallel = compare(entries_a, entries_b, HashCache(), io_workers=4)

        self.assertEqual(self._signature(sequential), self._signature(parallel))
        self.assertEqual(len(sequential.errors), len(parallel.errors))

    def test_warm_persistent_cache_matches_a_cold_cache(self) -> None:
        entries_a, entries_b = self._build_scenario_tree()
        db_path = os.path.join(self.root, "hashes.db")

        store = Store(db_path)
        warm_cache = PersistentHashCache(store)
        compare(entries_a, entries_b, warm_cache)  # populate the cache
        warm_cache.close()

        store2 = Store(db_path)
        resumed_cache = PersistentHashCache(store2)
        warm = compare(entries_a, entries_b, resumed_cache)
        resumed_cache.close()

        cold = compare(entries_a, entries_b, HashCache())

        self.assertEqual(self._signature(warm), self._signature(cold))


class BytesProgressTests(TempTreeCase):
    def test_bytes_to_resolve_excludes_fast_path_and_bytes_resolved_reaches_it(self) -> None:
        build_tree(
            self.root,
            {"a/unique.bin": b"x" * 999, "a/dup.bin": b"y" * 50, "b/dup.bin": b"y" * 50},
        )
        entries_a = _entries(self.root, ["a/unique.bin", "a/dup.bin"])
        entries_b = _entries(self.root, ["b/dup.bin"])
        snapshots: list[ScanProgress] = []

        compare(entries_a, entries_b, HashCache(), progress=snapshots.append)

        last = snapshots[-1]
        self.assertEqual(last.bytes_to_resolve, 50 + 50)  # the 999-byte fast-path file excluded
        self.assertEqual(last.bytes_resolved, last.bytes_to_resolve)

    def test_bytes_resolved_reaches_completion_with_a_fully_warm_cache(self) -> None:
        # Pins the bug the spec calls out: HashCache.bytes_read stays at 0 on
        # an all-cache-hit run, so bytes_resolved must be tracked from
        # FileEntry.size, or a resumed scan would look like it made no
        # progress at all.
        build_tree(self.root, {"a/dup.bin": b"y" * 50, "b/dup.bin": b"y" * 50})
        entries_a = _entries(self.root, ["a/dup.bin"])
        entries_b = _entries(self.root, ["b/dup.bin"])
        db_path = os.path.join(self.root, "hashes.db")

        store = Store(db_path)
        warm_up = PersistentHashCache(store)
        compare(entries_a, entries_b, warm_up)
        warm_up.close()

        store2 = Store(db_path)
        resumed = PersistentHashCache(store2)
        snapshots: list[ScanProgress] = []
        compare(entries_a, entries_b, resumed, progress=snapshots.append)
        resumed.close()

        self.assertEqual(resumed.bytes_read, 0)
        self.assertEqual(snapshots[-1].bytes_resolved, snapshots[-1].bytes_to_resolve)
        self.assertGreater(snapshots[-1].bytes_to_resolve, 0)


class SampledPreFilterTests(TempTreeCase):
    """The sampled stage must eliminate work without ever changing a verdict."""

    def _build_prefix_collision_tree(self):
        # Same size, same first 64 KiB, different tails -- the case that
        # forces a full read today.
        prefix = os.urandom(65536)
        build_tree(
            self.root,
            {"a/one.bin": prefix + b"A" * 4096, "a/two.bin": prefix + b"B" * 4096},
        )
        return _entries(self.root, ["a/one.bin", "a/two.bin"])

    def test_large_differing_files_are_resolved_without_a_full_hash(self) -> None:
        entries_a = self._build_prefix_collision_tree()
        cache = HashCache()

        with mock.patch("dupefinder.comparer.SAMPLE_THRESHOLD", 1024):
            report = compare(entries_a, [], cache)

        rows = _rows_by_path(report)
        self.assertEqual(rows["a/one.bin"].status, "exclusive")
        self.assertEqual(rows["a/two.bin"].status, "exclusive")
        self.assertEqual(cache.sampled_calls, 2)
        self.assertEqual(cache.full_calls, 0)  # the whole point: no full read

    def test_identical_large_files_still_fall_through_to_the_full_hash(self) -> None:
        # A sampled match proves nothing on its own, so it must NOT short
        # circuit -- the full hash is what decides identity.
        content = os.urandom(65536) + b"tail"
        build_tree(self.root, {"a/one.bin": content, "a/two.bin": content})
        entries_a = _entries(self.root, ["a/one.bin", "a/two.bin"])
        cache = HashCache()

        with mock.patch("dupefinder.comparer.SAMPLE_THRESHOLD", 1024):
            report = compare(entries_a, [], cache)

        rows = _rows_by_path(report)
        self.assertEqual(rows["a/one.bin"].status, "exclusive")
        self.assertEqual(rows["a/two.bin"].status, "internal_copy")
        self.assertEqual(rows["a/two.bin"].duplicate_of, "a/one.bin")
        self.assertEqual(cache.full_calls, 2)

    def test_files_below_the_threshold_never_use_the_sampled_stage(self) -> None:
        # Identical tiny files land in the same partial bucket, so they DO
        # reach the point where the sampled stage would fire -- and must skip
        # it on size, going straight to the full hash as before.
        build_tree(self.root, {"a/one.bin": b"same tiny content", "a/two.bin": b"same tiny content"})
        entries_a = _entries(self.root, ["a/one.bin", "a/two.bin"])
        cache = HashCache()

        report = compare(entries_a, [], cache)  # real 8 MiB threshold, tiny files

        self.assertEqual(cache.sampled_calls, 0)
        self.assertEqual(cache.full_calls, 2)
        rows = _rows_by_path(report)
        self.assertEqual(rows["a/two.bin"].status, "internal_copy")

    def test_verdicts_match_a_run_with_the_sampled_stage_disabled(self) -> None:
        # The pre-filter is an optimisation: identical classification, fewer
        # bytes read. An unreachable threshold disables it entirely.
        entries_a = self._build_prefix_collision_tree()

        with mock.patch("dupefinder.comparer.SAMPLE_THRESHOLD", 1024):
            with_filter = compare(entries_a, [], HashCache())
        with mock.patch("dupefinder.comparer.SAMPLE_THRESHOLD", 1 << 60):
            without_filter = compare(entries_a, [], HashCache())

        self.assertEqual(
            [(r.rel_path, r.status, r.duplicate_of) for r in with_filter.rows],
            [(r.rel_path, r.status, r.duplicate_of) for r in without_filter.rows],
        )


class HashManyGeneratorInputTests(unittest.TestCase):
    def test_generator_input_under_threading_does_not_silently_drop_items(self) -> None:
        # Regression test: the threaded branch of _hash_many used to iterate
        # `items` twice -- once via executor.map, once via zip. Executor.map
        # eagerly drains its input, so a lazy iterable (e.g. a generator, as
        # opposed to the lists both real call sites happen to pass today)
        # would already be exhausted by the time zip ran over it, and zip
        # would then silently yield zero pairs. Every file in that bucket
        # would vanish from the results with no exception and no error
        # logged. _hash_many now materializes `items` into a list as its
        # first statement, so this must produce every entry even when a
        # generator is passed in and an executor is active.
        entries = [
            FileEntry(abs_path=f"/fake/path{i}", rel_path=f"path{i}", size=i)
            for i in range(5)
        ]
        items = ((entry, "a") for entry in entries)  # a generator, not a list

        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(_hash_many(items, lambda path: path, executor))

        self.assertEqual(len(results), 5)
        result_rel_paths = {entry.rel_path for entry, _origin, _result in results}
        self.assertEqual(result_rel_paths, {entry.rel_path for entry in entries})


if __name__ == "__main__":
    unittest.main()
