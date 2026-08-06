"""Tests for dupefinder.models."""

import unittest

from dupefinder.models import DuplicateGroup, ReportRow


def _row(rel_path: str, size: int = 100) -> ReportRow:
    return ReportRow(
        id=rel_path, abs_path=f"/root/{rel_path}", rel_path=rel_path, size=size,
        status="internal_copy", sha256="deadbeef",
    )


class DuplicateGroupTests(unittest.TestCase):
    def test_wasted_bytes_is_size_times_extra_copies(self) -> None:
        group = DuplicateGroup(
            digest="deadbeef",
            size=100,
            members=[_row("a.txt"), _row("b.txt"), _row("c.txt")],
        )

        self.assertEqual(group.wasted_bytes, 200)  # 3 copies, 1 kept -> 2 * 100

    def test_wasted_bytes_is_zero_for_a_two_member_group_of_zero_byte_files(self) -> None:
        group = DuplicateGroup(digest="e3b0c442", size=0, members=[_row("x", 0), _row("y", 0)])

        self.assertEqual(group.wasted_bytes, 0)


if __name__ == "__main__":
    unittest.main()
