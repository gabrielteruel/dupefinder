"""Tests for dupefinder.mover."""

import json
import os
import unittest

from dupefinder.hashing import HashCache
from dupefinder.models import NoisyDir
from dupefinder.mover import apply_moves, move_to_trash, validate_paths, write_report
from tests.helpers import TempTreeCase, build_tree


class ApplyMovesTests(TempTreeCase):
    def test_nested_relative_path_is_recreated_under_destination(self) -> None:
        build_tree(self.root, {"a/photos/2024/beach.jpg": b"photo bytes"})
        dest = os.path.join(self.root, "dest")
        source = os.path.join(self.root, "a", "photos", "2024", "beach.jpg")

        result = apply_moves([(source, "photos/2024/beach.jpg")], dest, HashCache())

        expected_target = os.path.join(dest, "photos", "2024", "beach.jpg")
        self.assertEqual(len(result.moved), 1)
        self.assertEqual(result.moved[0]["dest_path"], expected_target)
        self.assertTrue(os.path.isfile(expected_target))
        self.assertFalse(os.path.exists(source))

    def test_collision_with_identical_content_is_skipped(self) -> None:
        build_tree(
            self.root,
            {
                "a/file.txt": b"same content",
                "dest/file.txt": b"same content",
            },
        )
        source = os.path.join(self.root, "a", "file.txt")
        dest = os.path.join(self.root, "dest")

        result = apply_moves([(source, "file.txt")], dest, HashCache())

        self.assertEqual(len(result.skipped_identical), 1)
        self.assertEqual(result.moved, [])
        self.assertTrue(os.path.exists(source), "source must be left untouched in A")

    def test_collision_with_different_content_is_moved_with_suffix(self) -> None:
        build_tree(
            self.root,
            {
                "a/file.txt": b"new content",
                "dest/file.txt": b"old different content",
            },
        )
        source = os.path.join(self.root, "a", "file.txt")
        dest = os.path.join(self.root, "dest")

        result = apply_moves([(source, "file.txt")], dest, HashCache())

        self.assertEqual(len(result.renamed), 1)
        expected_target = os.path.join(dest, "file_1.txt")
        self.assertEqual(result.renamed[0]["dest_path"], expected_target)
        self.assertTrue(os.path.isfile(expected_target))
        self.assertFalse(os.path.exists(source))


class ValidatePathsTests(TempTreeCase):
    def setUp(self) -> None:
        super().setUp()
        build_tree(self.root, {"a/.keep": b"", "b/.keep": b""})
        self.a = os.path.join(self.root, "a")
        self.b = os.path.join(self.root, "b")

    def test_rejects_dest_inside_a(self) -> None:
        dest = os.path.join(self.a, "dest")
        with self.assertRaises(ValueError):
            validate_paths(self.a, self.b, dest)

    def test_rejects_dest_inside_b(self) -> None:
        dest = os.path.join(self.b, "dest")
        with self.assertRaises(ValueError):
            validate_paths(self.a, self.b, dest)

    def test_rejects_dest_equal_to_a(self) -> None:
        with self.assertRaises(ValueError):
            validate_paths(self.a, self.b, self.a)

    def test_rejects_a_inside_b(self) -> None:
        nested_a = os.path.join(self.b, "nested_a")
        os.makedirs(nested_a)
        dest = os.path.join(self.root, "dest")
        with self.assertRaises(ValueError):
            validate_paths(nested_a, self.b, dest)

    def test_accepts_a_valid_combination(self) -> None:
        dest = os.path.join(self.root, "dest")
        validate_paths(self.a, self.b, dest)  # must not raise


class MoveToTrashTests(TempTreeCase):
    def test_relocates_directory_under_dest_trash_preserving_structure(self) -> None:
        build_tree(self.root, {"a/.git/config": b"git config"})
        dest = os.path.join(self.root, "dest")
        noisy = NoisyDir(
            abs_path=os.path.join(self.root, "a", ".git"),
            rel_path=".git",
            root="A",
            reason="vcs",
            file_count=1,
            total_bytes=10,
        )

        trashed = move_to_trash([noisy], dest)

        expected_target = os.path.join(dest, "_trash", ".git")
        self.assertEqual(trashed[0]["dest_path"], expected_target)
        self.assertTrue(os.path.isfile(os.path.join(expected_target, "config")))

    def test_raises_value_error_for_a_root_b_entry(self) -> None:
        noisy = NoisyDir(
            abs_path="/somewhere/b/.git",
            rel_path=".git",
            root="B",
            reason="vcs",
            file_count=1,
            total_bytes=10,
        )
        dest = os.path.join(self.root, "dest")

        with self.assertRaises(ValueError):
            move_to_trash([noisy], dest)


class WriteReportTests(TempTreeCase):
    def test_produces_valid_rereadable_json_at_expected_path(self) -> None:
        os.makedirs(os.path.join(self.root, "dest"))
        dest = os.path.join(self.root, "dest")
        payload = {"moved": [{"rel_path": "x.txt"}], "stats": {"files_a": 1}}

        report_path = write_report(dest, payload)

        self.assertTrue(report_path.startswith(dest))
        self.assertTrue(os.path.basename(report_path).startswith("_report_"))
        self.assertTrue(report_path.endswith(".json"))
        with open(report_path, encoding="utf-8") as f:
            loaded = json.load(f)
        self.assertEqual(loaded, payload)


if __name__ == "__main__":
    unittest.main()
