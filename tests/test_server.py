"""Tests for dupefinder.server."""

import os
import unittest
from unittest import mock

from dupefinder.models import Report, ReportRow, Stats
from dupefinder.server import (
    JOBS,
    Busy,
    Job,
    _drive_shortcuts,
    _single_flight,
    handle_apply,
    handle_scan,
)
from tests.helpers import TempTreeCase, build_tree


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


class SingleFlightTests(unittest.TestCase):
    def test_refuses_a_second_entry_for_the_same_key(self) -> None:
        with _single_flight("k"):
            with self.assertRaises(Busy):
                with _single_flight("k"):
                    pass

    def test_key_is_released_even_when_the_body_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            with _single_flight("k"):
                raise RuntimeError("boom")

        with _single_flight("k"):  # must not raise Busy
            pass

    def test_different_keys_do_not_block_each_other(self) -> None:
        with _single_flight("a"):
            with _single_flight("b"):
                pass


class JobsTestCase(TempTreeCase):
    """Base case that clears the global job registry around each test."""

    def setUp(self) -> None:
        super().setUp()
        JOBS.clear()

    def tearDown(self) -> None:
        JOBS.clear()
        super().tearDown()


class ScanDeduplicationTests(JobsTestCase):
    def setUp(self) -> None:
        super().setUp()
        build_tree(self.root, {"a/f.txt": b"1", "b/g.txt": b"2"})
        self.a = os.path.join(self.root, "a")
        self.b = os.path.join(self.root, "b")

    def test_identical_request_while_running_reuses_the_same_job(self) -> None:
        # A repeated click must not spawn a second scanning thread.
        JOBS["existing"] = Job(
            id="existing",
            status="running",
            config={"a": self.a, "b": self.b, "rules": {}},
        )

        status, payload = handle_scan({"a": self.a, "b": self.b, "rules": {}})

        self.assertEqual(status, 200)
        self.assertEqual(payload["job_id"], "existing")
        self.assertEqual(len(JOBS), 1)

    def test_different_rules_start_a_separate_job(self) -> None:
        JOBS["existing"] = Job(
            id="existing",
            status="running",
            config={"a": self.a, "b": self.b, "rules": {}},
        )

        status, payload = handle_scan(
            {"a": self.a, "b": self.b, "rules": {"/some/dir": "skip"}}
        )

        self.assertEqual(status, 200)
        self.assertNotEqual(payload["job_id"], "existing")

    def test_finished_job_does_not_block_a_new_scan(self) -> None:
        JOBS["old"] = Job(
            id="old",
            status="done",
            config={"a": self.a, "b": self.b, "rules": {}},
        )

        status, payload = handle_scan({"a": self.a, "b": self.b, "rules": {}})

        self.assertEqual(status, 200)
        self.assertNotEqual(payload["job_id"], "old")


class ApplyOnceTests(JobsTestCase):
    def setUp(self) -> None:
        super().setUp()
        build_tree(self.root, {"a/keep.txt": b"payload", "b/other.txt": b"x"})
        self.a = os.path.join(self.root, "a")
        self.b = os.path.join(self.root, "b")
        self.dest = os.path.join(self.root, "dest")

        row = ReportRow(
            id="keep.txt",
            abs_path=os.path.join(self.a, "keep.txt"),
            rel_path="keep.txt",
            size=7,
            status="exclusive",
        )
        JOBS["job1"] = Job(
            id="job1",
            status="done",
            report=Report(rows=[row], errors=[], stats=Stats()),
            config={"a": self.a, "b": self.b, "rules": {}},
        )

    def _apply(self):
        return handle_apply(
            {"job_id": "job1", "dest": self.dest, "selected": ["keep.txt"]}
        )

    def test_second_apply_is_refused(self) -> None:
        # Moving is destructive and not idempotent: the second pass would find
        # its source already gone, so it must be rejected outright.
        first_status, first_payload = self._apply()
        self.assertEqual(first_status, 200)
        self.assertEqual(len(first_payload["moved"]), 1)

        second_status, second_payload = self._apply()

        self.assertEqual(second_status, 409)
        self.assertIn("already been applied", second_payload["error"])

    def test_apply_in_progress_is_refused(self) -> None:
        JOBS["job1"].applying = True

        status, payload = self._apply()

        self.assertEqual(status, 409)
        self.assertIn("already being moved", payload["error"])

    def test_failed_apply_releases_the_in_progress_flag(self) -> None:
        with mock.patch("dupefinder.server.apply_moves", side_effect=OSError("disk gone")):
            with self.assertRaises(OSError):
                self._apply()

        self.assertFalse(JOBS["job1"].applying)
        self.assertFalse(JOBS["job1"].applied)


if __name__ == "__main__":
    unittest.main()
