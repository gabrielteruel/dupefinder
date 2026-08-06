"""Tests for dupefinder.keeprules.

Pure functions: no filesystem, no server, no clock. This is where the
subtle tie-break logic lives -- see docs/superpowers/specs/2026-08-05-dedupe-mode-design.md
section 5 for the rules these tests pin down.
"""

import unittest

from dupefinder.keeprules import group_duplicates, resolve
from dupefinder.models import ReportRow


def _row(rel_path: str, sha256: str | None, size: int = 100, status: str = "internal_copy") -> ReportRow:
    return ReportRow(
        id=rel_path, abs_path=f"/root/{rel_path}", rel_path=rel_path, size=size,
        status=status, sha256=sha256,
    )


class GroupDuplicatesTests(unittest.TestCase):
    def test_groups_rows_sharing_a_digest(self) -> None:
        rows = [
            _row("a/x.png", "hash1"),
            _row("b/x.png", "hash1"),
            _row("unique.txt", "hash2", status="exclusive"),
        ]

        groups, empty_group = group_duplicates(rows)

        self.assertIsNone(empty_group)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].digest, "hash1")
        self.assertEqual([m.rel_path for m in groups[0].members], ["a/x.png", "b/x.png"])

    def test_rows_with_no_digest_are_never_grouped(self) -> None:
        # sha256 is None: the fast path already proved this file unique by
        # size alone, with zero bytes read. It cannot be a duplicate of
        # anything and must not appear in any group.
        rows = [_row("solo.txt", None, status="exclusive")]

        groups, empty_group = group_duplicates(rows)

        self.assertEqual(groups, [])
        self.assertIsNone(empty_group)

    def test_a_digest_with_only_one_row_is_not_a_group(self) -> None:
        rows = [_row("a/x.png", "hash1"), _row("b/y.png", "hash2")]

        groups, _empty_group = group_duplicates(rows)

        self.assertEqual(groups, [])

    def test_members_are_sorted_by_rel_path(self) -> None:
        rows = [_row("z/x.png", "hash1"), _row("a/x.png", "hash1")]

        groups, _empty_group = group_duplicates(rows)

        self.assertEqual([m.rel_path for m in groups[0].members], ["a/x.png", "z/x.png"])

    def test_groups_are_sorted_by_wasted_bytes_descending(self) -> None:
        rows = [
            _row("small/1.txt", "hash-small", size=10),
            _row("small/2.txt", "hash-small", size=10),
            _row("big/1.txt", "hash-big", size=1000),
            _row("big/2.txt", "hash-big", size=1000),
        ]

        groups, _empty_group = group_duplicates(rows)

        self.assertEqual([g.digest for g in groups], ["hash-big", "hash-small"])

    def test_zero_byte_group_is_split_out_separately(self) -> None:
        rows = [
            _row(".gitkeep", "e3b0c442", size=0),
            _row("src/.gitkeep", "e3b0c442", size=0),
            _row("a/x.png", "hash1", size=10),
            _row("b/x.png", "hash1", size=10),
        ]

        groups, empty_group = group_duplicates(rows)

        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].digest, "hash1")
        self.assertIsNotNone(empty_group)
        self.assertEqual(empty_group.digest, "e3b0c442")
        self.assertEqual(len(empty_group.members), 2)

    def test_no_zero_byte_files_means_no_empty_group(self) -> None:
        rows = [_row("a/x.png", "hash1", size=10), _row("b/x.png", "hash1", size=10)]

        _groups, empty_group = group_duplicates(rows)

        self.assertIsNone(empty_group)


class ResolveTests(unittest.TestCase):
    def test_lowest_priority_index_wins(self) -> None:
        rows = [
            _row("fotos-ordenadas/bici/img.png", "h1"),
            _row("2019/ev2/bici/img.png", "h1"),
            _row("backup/viejo/img.png", "h1"),
        ]
        groups, _empty_group = group_duplicates(rows)

        kept = resolve(groups, keep_rules=["fotos-ordenadas", "2019", "backup"])

        self.assertEqual(kept["h1"], "fotos-ordenadas/bici/img.png")

    def test_rule_order_determines_the_winner_not_list_order(self) -> None:
        rows = [_row("2019/img.png", "h1"), _row("fotos-ordenadas/img.png", "h1")]
        groups, _empty_group = group_duplicates(rows)

        kept = resolve(groups, keep_rules=["fotos-ordenadas", "2019"])

        self.assertEqual(kept["h1"], "fotos-ordenadas/img.png")

    def test_no_matching_rule_falls_back_to_shallowest_path(self) -> None:
        rows = [_row("docs/archive/2020/doc.pdf", "h1"), _row("docs/doc.pdf", "h1")]
        groups, _empty_group = group_duplicates(rows)

        kept = resolve(groups, keep_rules=["some/other/rule"])

        self.assertEqual(kept["h1"], "docs/doc.pdf")

    def test_equal_depth_falls_back_to_lexicographic_order(self) -> None:
        rows = [_row("z/doc.pdf", "h1"), _row("a/doc.pdf", "h1")]
        groups, _empty_group = group_duplicates(rows)

        kept = resolve(groups, keep_rules=[])

        self.assertEqual(kept["h1"], "a/doc.pdf")

    def test_rule_matches_by_path_component_not_string_prefix(self) -> None:
        # "fotos" must match "fotos/x.png" but NOT "fotos2/x.png" -- a naive
        # string prefix check would wrongly treat "fotos2" as inside "fotos".
        rows = [_row("fotos2/x.png", "h1"), _row("elsewhere/x.png", "h1")]
        groups, _empty_group = group_duplicates(rows)

        kept = resolve(groups, keep_rules=["fotos"])

        # Neither matches the rule, so this falls back to shallowest/lexicographic:
        # both are depth 2, "elsewhere" < "fotos2" lexicographically.
        self.assertEqual(kept["h1"], "elsewhere/x.png")

    def test_zero_byte_groups_are_never_resolved(self) -> None:
        rows = [_row(".gitkeep", "e3", size=0), _row("src/.gitkeep", "e3", size=0)]
        _groups, empty_group = group_duplicates(rows)

        kept = resolve([empty_group], keep_rules=[])

        self.assertEqual(kept, {})

    def test_result_is_deterministic_across_repeated_calls(self) -> None:
        rows = [
            _row("c/x.png", "h1"), _row("a/x.png", "h1"), _row("b/x.png", "h1"),
        ]
        groups, _empty_group = group_duplicates(rows)

        first = resolve(groups, keep_rules=["b"])
        second = resolve(groups, keep_rules=["b"])

        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
