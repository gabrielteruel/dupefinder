"""Tests for dupefinder.server."""

import os
import time
import unittest
from unittest import mock

from dupefinder import server
from dupefinder.diskinfo import VolumeInfo
from dupefinder.eta import EtaEstimator
from dupefinder.hashing import PersistentHashCache
from dupefinder.models import Report, ReportRow, Stats
from dupefinder.server import (
    JOBS,
    Busy,
    Job,
    _drive_shortcuts,
    _run_scan_job,
    _single_flight,
    handle_apply,
    handle_cache_clear,
    handle_cache_stats,
    handle_progress,
    handle_scan,
    handle_settings_get,
    handle_settings_post,
    handle_volumes,
)
from dupefinder.store import HashRow
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
            config={"a": self.a, "b": self.b, "rules": {}, "io_workers": 1, "use_cache": True},
        )

        status, payload = handle_scan({"a": self.a, "b": self.b, "rules": {}})

        self.assertEqual(status, 200)
        self.assertEqual(payload["job_id"], "existing")
        self.assertEqual(len(JOBS), 1)

    def test_different_rules_start_a_separate_job(self) -> None:
        JOBS["existing"] = Job(
            id="existing",
            status="running",
            config={"a": self.a, "b": self.b, "rules": {}, "io_workers": 1, "use_cache": True},
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
            config={"a": self.a, "b": self.b, "rules": {}, "io_workers": 1, "use_cache": True},
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
            config={"a": self.a, "b": self.b, "rules": {}, "io_workers": 1, "use_cache": True},
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


class CacheTestCase(JobsTestCase):
    """Points the persistent store at a temp dir and resets the singleton."""

    def setUp(self) -> None:
        super().setUp()
        server._STORE = None
        server._CACHE_DIR_OVERRIDE = self.root

    def tearDown(self) -> None:
        if server._STORE is not None:
            server._STORE.close()
        server._STORE = None
        server._CACHE_DIR_OVERRIDE = None
        super().tearDown()


class SettingsEndpointTests(CacheTestCase):
    def test_settings_round_trip_through_the_api(self) -> None:
        status, _ = handle_settings_post({"io_workers": 6, "use_cache": False})
        self.assertEqual(status, 200)

        status, payload = handle_settings_get()
        self.assertEqual(status, 200)
        self.assertEqual(payload["io_workers"], 6)
        self.assertEqual(payload["use_cache"], False)

    def test_defaults_when_nothing_has_been_saved_yet(self) -> None:
        status, payload = handle_settings_get()
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"last_paths": None, "io_workers": 1, "use_cache": True})

    def test_unknown_key_is_rejected(self) -> None:
        status, payload = handle_settings_post({"bogus": 1})
        self.assertEqual(status, 400)
        self.assertIn("bogus", payload["error"])


class CacheStatsEndpointTests(CacheTestCase):
    def test_stats_reflect_stored_rows(self) -> None:
        store = server._get_store()
        store.put_hash(HashRow(path="/x", size=1, mtime_ns=1, partial=None, full="h"))
        store.flush()

        status, payload = handle_cache_stats()

        self.assertEqual(status, 200)
        self.assertEqual(payload["row_count"], 1)
        self.assertGreater(payload["db_size_bytes"], 0)

    def test_clear_removes_all_rows(self) -> None:
        store = server._get_store()
        store.put_hash(HashRow(path="/x", size=1, mtime_ns=1, partial=None, full="h"))
        store.flush()

        status, _ = handle_cache_clear({})

        self.assertEqual(status, 200)
        self.assertEqual(server._get_store().row_count(), 0)


class VolumesEndpointTests(unittest.TestCase):
    def test_returns_detected_volumes_and_combined_suggestion(self) -> None:
        hdd = VolumeInfo(path="/a", kind="hdd", transport="usb", label="USB HDD", suggested_workers=1)
        ssd = VolumeInfo(path="/b", kind="ssd", transport="nvme", label="SSD", suggested_workers=4)

        with mock.patch("dupefinder.server.detect", side_effect=[hdd, ssd]):
            status, payload = handle_volumes({"a": "/a", "b": "/b"})

        self.assertEqual(status, 200)
        self.assertEqual(payload["suggested_workers"], 1)
        self.assertEqual(len(payload["volumes"]), 2)


class ScanIoWorkersValidationTests(JobsTestCase):
    def setUp(self) -> None:
        super().setUp()
        build_tree(self.root, {"a/f.txt": b"1", "b/g.txt": b"2"})
        self.a = os.path.join(self.root, "a")
        self.b = os.path.join(self.root, "b")

    def test_rejects_io_workers_above_the_max(self) -> None:
        status, payload = handle_scan({"a": self.a, "b": self.b, "io_workers": 33})
        self.assertEqual(status, 400)
        self.assertIn("io_workers", payload["error"])

    def test_rejects_io_workers_below_one(self) -> None:
        status, payload = handle_scan({"a": self.a, "b": self.b, "io_workers": 0})
        self.assertEqual(status, 400)

    def test_rejects_non_integer_io_workers(self) -> None:
        status, payload = handle_scan({"a": self.a, "b": self.b, "io_workers": "four"})
        self.assertEqual(status, 400)


class ProgressEtaTests(JobsTestCase):
    def test_eta_is_null_during_walk_phases(self) -> None:
        JOBS["j"] = Job(id="j", phase="scanning_a", bytes_to_resolve=1000)

        status, payload = handle_progress("j")

        self.assertEqual(status, 200)
        self.assertIsNone(payload["eta_seconds"])

    def test_eta_is_a_number_during_comparing_with_enough_history(self) -> None:
        job = Job(id="j", phase="comparing", bytes_to_resolve=1_000_000)
        now = time.monotonic()
        job.eta.observe(now - 10, 0)
        job.eta.observe(now, 500_000)
        JOBS["j"] = job

        status, payload = handle_progress("j")

        self.assertEqual(status, 200)
        self.assertIsInstance(payload["eta_seconds"], float)


class StoreReopensAfterCloseTests(CacheTestCase):
    """_run_scan_job's finally calls cache.close(), which closes the shared
    singleton's connection (see the comment in _run_scan_job). The very next
    request that needs the store -- another scan, or /api/settings, or
    /api/cache/* -- must not crash with "Cannot operate on a closed database";
    _get_store() must transparently hand back a working connection.
    """

    def test_get_store_reopens_a_connection_closed_by_a_finished_job(self) -> None:
        store = server._get_store()
        store.put_hash(HashRow(path="/x", size=1, mtime_ns=1, partial=None, full="h"))
        store.flush()
        store.close()  # simulates _run_scan_job's finally: cache.close()

        reopened = server._get_store()

        self.assertIsNot(reopened, store)
        self.assertEqual(reopened.row_count(), 1)


class CacheCloseOnFailureTests(JobsTestCase):
    def test_cache_is_closed_even_when_compare_raises(self) -> None:
        build_tree(self.root, {"a/f.txt": b"1", "b/g.txt": b"2"})
        a = os.path.join(self.root, "a")
        b = os.path.join(self.root, "b")
        JOBS["job-x"] = Job(
            id="job-x", config={"a": a, "b": b, "rules": {}, "io_workers": 1, "use_cache": True}
        )

        with (
            mock.patch("dupefinder.server.compare", side_effect=RuntimeError("boom")),
            mock.patch("dupefinder.server.PersistentHashCache.close") as mock_close,
            mock.patch("dupefinder.server._get_store", return_value=mock.Mock()),
        ):
            _run_scan_job("job-x", a, b, {}, io_workers=1, use_cache=True)

        self.assertEqual(JOBS["job-x"].status, "error")
        mock_close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
