"""Tests for dupefinder.server."""

import unittest
from unittest import mock

from dupefinder.server import _drive_shortcuts


class DriveShortcutsTests(unittest.TestCase):
    """The shortcuts must reflect what is really mounted, not what /mnt contains.

    Patches the filesystem calls so the result never depends on the host's /mnt.
    """

    def _run_with(self, names: list[str], mounted: set[str]) -> list[dict]:
        with (
            mock.patch("os.listdir", return_value=names),
            mock.patch("os.path.ismount", side_effect=lambda p: p in mounted),
        ):
            return _drive_shortcuts()

    def test_skips_single_letter_directory_that_is_not_a_mount_point(self) -> None:
        # /mnt/d is a stale mount point left behind by a previous session: it
        # exists as an empty directory but no drive is mounted there.
        result = self._run_with(["c", "d"], mounted={"/mnt/c"})

        self.assertEqual(result, [{"name": "Drive C:", "path": "/mnt/c"}])

    def test_lists_every_mounted_drive(self) -> None:
        result = self._run_with(["c", "d"], mounted={"/mnt/c", "/mnt/d"})

        self.assertEqual(
            result,
            [
                {"name": "Drive C:", "path": "/mnt/c"},
                {"name": "Drive D:", "path": "/mnt/d"},
            ],
        )

    def test_excludes_multi_letter_wsl_internal_mounts(self) -> None:
        result = self._run_with(
            ["c", "wsl", "wslg"], mounted={"/mnt/c", "/mnt/wsl", "/mnt/wslg"}
        )

        self.assertEqual(result, [{"name": "Drive C:", "path": "/mnt/c"}])

    def test_returns_empty_list_when_mnt_is_unavailable(self) -> None:
        with mock.patch("os.listdir", side_effect=OSError("no /mnt here")):
            self.assertEqual(_drive_shortcuts(), [])


if __name__ == "__main__":
    unittest.main()
