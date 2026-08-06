"""Tests for dupefinder.server."""

import os
import threading
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
    _run_compare_pipeline,
    _run_dedupe_job,
    _run_scan_job,
    _single_flight,
    handle_apply,
    handle_cache_clear,
    handle_cache_stats,
    handle_dedupe_apply,
    handle_dedupe_report,
    handle_dedupe_resolve,
    handle_dedupe_scan,
    handle_prescan,
    handle_progress,
    handle_report,
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


class DriveShortcutsWindowsTests(unittest.TestCase):
    def test_returns_existing_drive_letters_on_windows(self) -> None:
        existing = {"C:\\", "D:\\"}
        with (
            mock.patch("os.name", "nt"),
            mock.patch("os.path.exists", side_effect=lambda p: p in existing),
        ):
            result = _drive_shortcuts()

        self.assertEqual(
            result, [{"name": "Drive C:", "path": "C:\\"}, {"name": "Drive D:", "path": "D:\\"}]
        )


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
    """Base case that clears the global job registry around each test, and
    points the persistent hash cache at this test's own temp dir instead of
    the user's real ~/.cache/dupefinder.

    Every test built on this class is a candidate for triggering a real scan
    job (handle_scan defaults use_cache=True and spawns a background thread
    running _run_scan_job -> PersistentHashCache(_open_job_store())), so the
    override belongs here rather than in a narrower subclass -- without it,
    the suite writes outside its own temp directory and can't run on a
    machine where ~/.cache isn't writable.

    handle_scan's scan thread is daemon=True and this test suite never joins
    it directly, so it can still be alive after the test method returns. If
    tearDown reset _CACHE_DIR_OVERRIDE and deleted the temp dir first, that
    still-running thread would read _CACHE_DIR_OVERRIDE back as None the
    moment it got around to calling _open_job_store(), and fall through to
    the user's real cache directory -- the same non-hermeticity this class
    exists to prevent, just delayed instead of avoided. So tearDown joins
    any thread that appeared during the test before touching global state.
    """

    def setUp(self) -> None:
        super().setUp()
        JOBS.clear()
        self._threads_before = set(threading.enumerate())
        server._STORE = None
        server._CACHE_DIR_OVERRIDE = self.root

    def tearDown(self) -> None:
        spawned = set(threading.enumerate()) - self._threads_before
        for t in spawned:
            t.join(timeout=5)
        JOBS.clear()
        if server._STORE is not None:
            server._STORE.close()
        server._STORE = None
        server._CACHE_DIR_OVERRIDE = None
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


class PrescanSingleFolderTests(JobsTestCase):
    def setUp(self) -> None:
        super().setUp()
        build_tree(self.root, {"a/.git/config": b"x", "a/photo.png": b"y"})
        self.a = os.path.join(self.root, "a")

    def test_prescan_accepts_a_request_with_no_b(self) -> None:
        status, payload = handle_prescan({"a": self.a})

        self.assertEqual(status, 200)
        self.assertEqual(len(payload["noisy"]), 1)
        self.assertEqual(payload["noisy"][0]["root"], "A")


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
    """Marker subclass for tests that exercise the persistent cache directly.

    JobsTestCase itself now points the persistent store at a temp dir and
    resets the singleton around every test (see its docstring), so this
    class no longer needs to duplicate that setup -- it exists purely to
    group the cache-focused test classes below under a clearer name.
    """


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


class DedupeScanTests(JobsTestCase):
    def setUp(self) -> None:
        super().setUp()
        build_tree(self.root, {"folder/a.txt": b"1", "folder/b.txt": b"2"})
        self.folder = os.path.join(self.root, "folder")

    def test_creates_a_job_with_dedupe_mode(self) -> None:
        status, payload = handle_dedupe_scan({"folder": self.folder})

        self.assertEqual(status, 200)
        job = JOBS[payload["job_id"]]
        self.assertEqual(job.mode, "dedupe")

    def test_rejects_a_nonexistent_folder(self) -> None:
        status, payload = handle_dedupe_scan({"folder": os.path.join(self.root, "missing")})

        self.assertEqual(status, 400)
        self.assertIn("error", payload)

    def test_rejects_io_workers_above_the_max(self) -> None:
        status, payload = handle_dedupe_scan({"folder": self.folder, "io_workers": 99})

        self.assertEqual(status, 400)
        self.assertIn("io_workers", payload["error"])

    def test_end_to_end_scan_finds_a_duplicate_pair(self) -> None:
        # Calls _run_dedupe_job directly and synchronously, exactly like
        # CacheCloseOnFailureTests does for _run_scan_job (test_server.py:381):
        # this is the established way to assert on a finished job's state
        # without threading a wait for the background thread into the test.
        build_tree(self.root, {"folder/sub/copy.txt": b"1"})  # duplicate of a.txt
        JOBS["j"] = Job(id="j", mode="dedupe", config={"folder": self.folder})

        _run_dedupe_job("j", self.folder, {}, io_workers=1, use_cache=False)

        job = JOBS["j"]
        self.assertEqual(job.status, "done", job.error)
        digests = [row.sha256 for row in job.report.rows if row.sha256]
        self.assertEqual(len(digests), 2)  # a.txt and sub/copy.txt share one digest


class ModeGuardTests(JobsTestCase):
    def setUp(self) -> None:
        super().setUp()
        JOBS["compare-job"] = Job(id="compare-job", mode="compare", status="done",
                                   report=Report(rows=[], errors=[], stats=Stats()))
        JOBS["dedupe-job"] = Job(id="dedupe-job", mode="dedupe", status="done",
                                  report=Report(rows=[], errors=[], stats=Stats()))

    def test_compare_report_rejects_a_dedupe_job(self) -> None:
        status, payload = handle_report("dedupe-job")

        self.assertEqual(status, 409)
        self.assertIn("error", payload)

    def test_dedupe_report_rejects_a_compare_job(self) -> None:
        status, payload = handle_dedupe_report("compare-job")

        self.assertEqual(status, 409)
        self.assertIn("error", payload)

    def test_dedupe_apply_rejects_a_compare_job(self) -> None:
        status, payload = handle_dedupe_apply(
            {"job_id": "compare-job", "dest": "/tmp/x", "selected": []}
        )

        self.assertEqual(status, 409)

    def test_compare_apply_rejects_a_dedupe_job(self) -> None:
        status, payload = handle_apply({"job_id": "dedupe-job", "dest": "/tmp/x", "selected": []})

        self.assertEqual(status, 409)


class DedupeReportTests(JobsTestCase):
    def test_returns_groups_and_a_separate_empty_group(self) -> None:
        rows = [
            ReportRow(id="a/x.png", abs_path="/root/a/x.png", rel_path="a/x.png",
                      size=10, status="internal_copy", sha256="h1"),
            ReportRow(id="b/x.png", abs_path="/root/b/x.png", rel_path="b/x.png",
                      size=10, status="exclusive", sha256="h1"),
            ReportRow(id=".gitkeep", abs_path="/root/.gitkeep", rel_path=".gitkeep",
                      size=0, status="internal_copy", sha256="e3"),
            ReportRow(id="src/.gitkeep", abs_path="/root/src/.gitkeep", rel_path="src/.gitkeep",
                      size=0, status="exclusive", sha256="e3"),
        ]
        JOBS["j"] = Job(id="j", mode="dedupe", status="done",
                         report=Report(rows=rows, errors=[], stats=Stats()))

        status, payload = handle_dedupe_report("j")

        self.assertEqual(status, 200)
        self.assertEqual(len(payload["groups"]), 1)
        self.assertEqual(payload["groups"][0]["digest"], "h1")
        self.assertEqual(payload["groups"][0]["wasted_bytes"], 10)
        self.assertIsNotNone(payload["empty_group"])
        self.assertEqual(payload["empty_group"]["digest"], "e3")

    def test_unknown_job_is_404(self) -> None:
        status, payload = handle_dedupe_report("missing")

        self.assertEqual(status, 404)

    def test_unfinished_job_is_409(self) -> None:
        JOBS["j"] = Job(id="j", mode="dedupe", status="running")

        status, payload = handle_dedupe_report("j")

        self.assertEqual(status, 409)


class DedupeResolveTests(JobsTestCase):
    def setUp(self) -> None:
        super().setUp()
        rows = [
            ReportRow(id="fotos-ordenadas/x.png", abs_path="/r/fotos-ordenadas/x.png",
                      rel_path="fotos-ordenadas/x.png", size=10, status="internal_copy", sha256="h1"),
            ReportRow(id="2019/x.png", abs_path="/r/2019/x.png",
                      rel_path="2019/x.png", size=10, status="exclusive", sha256="h1"),
        ]
        JOBS["j"] = Job(id="j", mode="dedupe", status="done",
                         report=Report(rows=rows, errors=[], stats=Stats()))

    def test_resolves_using_the_given_keep_rules(self) -> None:
        status, payload = handle_dedupe_resolve({"job_id": "j", "keep_rules": ["fotos-ordenadas"]})

        self.assertEqual(status, 200)
        self.assertEqual(payload["kept"]["h1"], "fotos-ordenadas/x.png")

    def test_empty_rule_string_is_rejected(self) -> None:
        status, payload = handle_dedupe_resolve({"job_id": "j", "keep_rules": [""]})

        self.assertEqual(status, 400)
        self.assertIn("error", payload)

    def test_whitespace_only_rule_string_is_rejected(self) -> None:
        status, payload = handle_dedupe_resolve({"job_id": "j", "keep_rules": ["   "]})

        self.assertEqual(status, 400)
        self.assertIn("error", payload)

    def test_rejects_a_compare_mode_job(self) -> None:
        JOBS["compare-job"] = Job(id="compare-job", mode="compare", status="done",
                                   report=Report(rows=[], errors=[], stats=Stats()))

        status, payload = handle_dedupe_resolve({"job_id": "compare-job", "keep_rules": []})

        self.assertEqual(status, 409)

    def test_unknown_job_is_404(self) -> None:
        status, payload = handle_dedupe_resolve({"job_id": "missing", "keep_rules": []})

        self.assertEqual(status, 404)


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
            mock.patch("dupefinder.server._open_job_store", return_value=mock.Mock()),
        ):
            _run_scan_job("job-x", a, b, {}, io_workers=1, use_cache=True)

        self.assertEqual(JOBS["job-x"].status, "error")
        mock_close.assert_called_once()


class ConcurrentJobCachesTests(CacheTestCase):
    """Two different-config scans are allowed to run at the same time (only
    identical-config requests are deduped -- see ScanDeduplicationTests), and
    each job's finally always calls cache.close(). If jobs shared one Store,
    the first job to finish would close the connection the other is still
    hashing through. _open_job_store() gives every job its own connection to
    the same db file instead, so this must be impossible.
    """

    def test_open_job_store_returns_independent_connections(self) -> None:
        store_a = server._open_job_store()
        store_b = server._open_job_store()

        self.assertIsNot(store_a, store_b)

    def test_closing_one_jobs_cache_does_not_break_another_still_open_one(self) -> None:
        cache_a = PersistentHashCache(server._open_job_store())
        cache_b = PersistentHashCache(server._open_job_store())

        cache_a.close()  # simulates job A's finally: cache.close()

        # Job B, still running, must be able to keep hashing through its own,
        # separate connection -- and its result must actually persist.
        path = os.path.join(self.root, "still_running.txt")
        with open(path, "wb") as f:
            f.write(b"still hashing")
        digest = cache_b.partial(path)  # must not raise
        self.assertTrue(digest)
        cache_b.close()

        reopened = server._open_job_store()
        row = reopened.get_hash(path, os.stat(path).st_size, os.stat(path).st_mtime_ns)
        self.assertIsNotNone(row)
        reopened.close()


class ConcurrentScanJobsCacheTests(CacheTestCase):
    """Full-integration version of ConcurrentJobCachesTests: two real
    _run_scan_job calls, run on separate threads, deliberately overlapped so
    that job A finishes (and closes its own cache) while job B is still
    inside compare() using its own. Ordering is forced with threading.Event
    rather than sleep(), so this is not timing-dependent/flaky.
    """

    def test_job_a_finishing_does_not_break_a_still_running_job_b(self) -> None:
        build_tree(
            self.root,
            {
                "a1/f.txt": b"1",
                "b1/g.txt": b"2",
                "a2/f.txt": b"3",
                "b2/g.txt": b"4",
            },
        )
        a1, b1 = os.path.join(self.root, "a1"), os.path.join(self.root, "b1")
        a2, b2 = os.path.join(self.root, "a2"), os.path.join(self.root, "b2")

        b_entered_compare = threading.Event()
        a_finished = threading.Event()
        real_compare = server.compare

        def fake_compare(entries_a, entries_b, cache, **kwargs):
            paths = [e.abs_path for e in entries_a] + [e.abs_path for e in entries_b]
            if any(a2 in p or b2 in p for p in paths):
                # This is job B's call. Prove we're holding our own cache/
                # connection already, then wait for job A to fully finish
                # (including its finally: cache.close()) before actually
                # hashing -- this is the exact overlap the bug depended on.
                b_entered_compare.set()
                a_finished.wait(timeout=5)
            return real_compare(entries_a, entries_b, cache, **kwargs)

        JOBS["job-a"] = Job(
            id="job-a", config={"a": a1, "b": b1, "rules": {}, "io_workers": 1, "use_cache": True}
        )
        JOBS["job-b"] = Job(
            id="job-b", config={"a": a2, "b": b2, "rules": {}, "io_workers": 1, "use_cache": True}
        )

        with mock.patch("dupefinder.server.compare", side_effect=fake_compare):
            thread_b = threading.Thread(
                target=_run_scan_job, args=("job-b", a2, b2, {}, 1, True)
            )
            thread_b.start()
            self.assertTrue(b_entered_compare.wait(timeout=5), "job B never entered compare()")

            _run_scan_job("job-a", a1, b1, {}, io_workers=1, use_cache=True)
            a_finished.set()

            thread_b.join(timeout=5)

        self.assertEqual(JOBS["job-a"].status, "done", JOBS["job-a"].error)
        self.assertEqual(JOBS["job-b"].status, "done", JOBS["job-b"].error)


if __name__ == "__main__":
    unittest.main()
