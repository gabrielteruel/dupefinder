"""Tests for dupefinder.scanner."""

import os
import unittest
import unittest.mock

from dupefinder.scanner import find_noisy_dirs, scan
from tests.helpers import TempTreeCase, build_tree


class ScanTests(TempTreeCase):
    def test_nested_tree_returns_correct_posix_rel_paths_sorted(self) -> None:
        build_tree(
            self.root,
            {
                "b.txt": b"1",
                "a/c.txt": b"2",
                "a/nested/d.txt": b"3",
            },
        )

        entries, errors = scan(self.root, skip_abs_paths=set())

        self.assertEqual(errors, [])
        rel_paths = [e.rel_path for e in entries]
        self.assertEqual(rel_paths, ["a/c.txt", "a/nested/d.txt", "b.txt"])
        for rel_path in rel_paths:
            self.assertNotIn(os.sep, rel_path.replace("/", ""))
            self.assertNotIn("\\", rel_path)

    def test_skip_abs_paths_prunes_directory_and_everything_under_it(self) -> None:
        build_tree(
            self.root,
            {
                "keep.txt": b"1",
                "skip_me/inner.txt": b"2",
                "skip_me/nested/deep.txt": b"3",
            },
        )
        skip_path = os.path.join(self.root, "skip_me")

        entries, _errors = scan(self.root, skip_abs_paths={skip_path})

        rel_paths = [e.rel_path for e in entries]
        self.assertEqual(rel_paths, ["keep.txt"])

    def test_symlinked_directory_is_not_descended_into_or_reported(self) -> None:
        build_tree(self.root, {"real/file.txt": b"1"})
        real_dir = os.path.join(self.root, "real")
        link_dir = os.path.join(self.root, "link")
        os.symlink(real_dir, link_dir, target_is_directory=True)

        entries, _errors = scan(self.root, skip_abs_paths=set())

        rel_paths = {e.rel_path for e in entries}
        self.assertEqual(rel_paths, {"real/file.txt"})


class ScanProgressTests(TempTreeCase):
    def test_progress_callback_reports_the_final_count(self) -> None:
        build_tree(self.root, {f"file{i}.txt": b"x" for i in range(5)})
        calls = []

        entries, _errors = scan(
            self.root, skip_abs_paths=set(), progress=lambda n, p: calls.append((n, p))
        )

        self.assertTrue(calls)
        self.assertEqual(calls[-1], (len(entries), ""))

    def test_progress_is_throttled_not_called_once_per_file(self) -> None:
        build_tree(self.root, {f"file{i}.txt": b"x" for i in range(50)})
        calls = []

        scan(self.root, skip_abs_paths=set(), progress=lambda n, p: calls.append((n, p)))

        # 50 files created near-instantly must not produce 50 callback calls;
        # the throttle collapses rapid completion into a handful of calls.
        self.assertLess(len(calls), 50)


class FindNoisyDirsTests(TempTreeCase):
    def test_reports_git_with_reason_count_and_bytes(self) -> None:
        build_tree(
            self.root,
            {
                "project/.git/config": b"x" * 10,
                "project/.git/objects/pack/pack.idx": b"y" * 5,
                "project/main.py": b"z",
            },
        )

        noisy = find_noisy_dirs(self.root, "A")

        git_entries = [n for n in noisy if n.rel_path.endswith(".git")]
        self.assertEqual(len(git_entries), 1)
        entry = git_entries[0]
        self.assertEqual(entry.reason, "vcs")
        self.assertEqual(entry.root, "A")
        self.assertEqual(entry.file_count, 2)
        self.assertEqual(entry.total_bytes, 15)

    def test_counts_are_exact_and_untruncated_below_the_limit(self) -> None:
        build_tree(
            self.root,
            {f".cache/file{i}.dat": b"xx" for i in range(5)},
        )

        noisy = find_noisy_dirs(self.root, "A")

        self.assertEqual(len(noisy), 1)
        self.assertEqual(noisy[0].file_count, 5)
        self.assertEqual(noisy[0].total_bytes, 10)
        self.assertFalse(noisy[0].counts_truncated)

    def test_counts_stop_at_the_limit_and_are_flagged_as_truncated(self) -> None:
        # A stand-in for the huge $RECYCLE.BIN that external NTFS drives carry:
        # walking it in full would stall the pre-scan over a slow filesystem.
        build_tree(
            self.root,
            {f".cache/file{i}.dat": b"x" for i in range(30)},
        )

        with unittest.mock.patch("dupefinder.scanner.COUNT_LIMIT", 10):
            noisy = find_noisy_dirs(self.root, "A")

        self.assertEqual(len(noisy), 1)
        self.assertEqual(noisy[0].file_count, 10)
        self.assertTrue(noisy[0].counts_truncated)

    def test_does_not_report_git_nested_inside_already_reported_node_modules(self) -> None:
        build_tree(
            self.root,
            {
                "node_modules/some_pkg/.git/config": b"x",
                "node_modules/some_pkg/index.js": b"y",
            },
        )

        noisy = find_noisy_dirs(self.root, "A")

        rel_paths = [n.rel_path for n in noisy]
        self.assertEqual(rel_paths, ["node_modules"])


if __name__ == "__main__":
    unittest.main()
