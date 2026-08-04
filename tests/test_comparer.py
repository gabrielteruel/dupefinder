"""Tests for dupefinder.comparer."""

import os
import unittest

from dupefinder.comparer import compare
from dupefinder.hashing import HashCache
from dupefinder.models import FileEntry
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


if __name__ == "__main__":
    unittest.main()
