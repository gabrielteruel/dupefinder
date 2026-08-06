"""ThreadingHTTPServer exposing the JSON API and serving the static web UI."""

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import traceback
import uuid
import webbrowser
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from dupefinder.comparer import compare
from dupefinder.diskinfo import combine, detect, is_wsl
from dupefinder.eta import EtaEstimator
from dupefinder.hashing import HashCache, PersistentHashCache
from dupefinder.keeprules import group_duplicates, resolve
from dupefinder.models import NoisyDir, Report
from dupefinder.mover import (
    apply_moves,
    move_to_trash,
    validate_paths,
    validate_sources,
    write_report,
)
from dupefinder.scanner import find_noisy_dirs, scan
from dupefinder.store import Store, cache_dir

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
DEFAULT_PORT = 8765


@dataclass
class Job:
    id: str
    mode: str = "compare"  # "compare" | "dedupe" -- guards which endpoints may touch this job
    status: str = "running"  # "running" | "done" | "error"
    phase: str = "scanning_a"  # "scanning_a" | "scanning_b" | "comparing" | "done"
    processed: int = 0
    total: int = 0  # 0 means indeterminate (scanning phases)
    bytes_resolved: int = 0
    bytes_to_resolve: int = 0
    current_path: str = ""
    cache_hits: int = 0
    cache_misses: int = 0
    started_at: float = field(default_factory=time.monotonic)
    report: Report | None = None
    error: str | None = None
    config: dict = field(default_factory=dict)  # {a, b, rules, io_workers, use_cache}
    trash_dirs: list[NoisyDir] = field(default_factory=list)  # internal: resolved at scan time
    applying: bool = False  # an apply is running right now
    applied: bool = False  # an apply already completed; never run a second one
    eta: EtaEstimator = field(default_factory=EtaEstimator)


JOBS: dict[str, Job] = {}
JOBS_LOCK = threading.Lock()

_STORE: Store | None = None
_STORE_LOCK = threading.Lock()
_CACHE_DIR_OVERRIDE: str | None = None  # set by --cache-dir


def _get_store() -> Store:
    """Return the shared singleton Store used by /api/settings and /api/cache/*.

    Never used by a scan job's cache -- see _open_job_store() below. This
    singleton is closed exactly once, at server shutdown (see main()), so it
    is always safe to keep reusing across requests.
    """
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            db_path = os.path.join(cache_dir(_CACHE_DIR_OVERRIDE), "hashes.db")
            _STORE = Store(db_path)
        return _STORE


def _open_job_store() -> Store:
    """Open a fresh, private Store connection for a single scan job's cache.

    Deliberately NOT the shared _get_store() singleton: handle_scan only
    dedupes identical-config requests, so two different-config scans can run
    concurrently, and _run_scan_job's finally always calls cache.close() (the
    single most important line for resumability -- an aborted scan must still
    keep everything it had already hashed). If two jobs shared one Store, the
    first job to finish would close the connection the other is still using
    mid-compare(), crashing it and, worse, discarding its not-yet-flushed
    hashes when its own finally then tried to flush through the dead
    connection. Giving every job its own connection to the same db file makes
    that impossible; WAL mode (see store.py) is exactly what makes multiple
    independent connections to one file safe to use at once.
    """
    db_path = os.path.join(cache_dir(_CACHE_DIR_OVERRIDE), "hashes.db")
    return Store(db_path)


class Busy(Exception):
    """Raised when an identical operation is already running.

    A user double-clicking a button must not start the same expensive walk
    twice, so the handler refuses the duplicate rather than racing it.
    """


_INFLIGHT: set[str] = set()
_INFLIGHT_LOCK = threading.Lock()


@contextmanager
def _single_flight(key: str):
    """Allow only one in-flight operation per key. Raises Busy on a duplicate."""
    with _INFLIGHT_LOCK:
        if key in _INFLIGHT:
            raise Busy("that operation is already running; wait for it to finish")
        _INFLIGHT.add(key)
    try:
        yield
    finally:
        with _INFLIGHT_LOCK:
            _INFLIGHT.discard(key)


# --------------------------------------------------------------------------
# Background scan worker
# --------------------------------------------------------------------------


def _run_compare_pipeline(
    job: Job,
    entries_a: list,
    entries_b: list,
    pre_errors: list,
    cache: HashCache,
    io_workers: int,
) -> None:
    """Shared tail of both _run_scan_job and _run_dedupe_job: run compare()
    against already-scanned entries and land the result on `job`.

    Extracted because this part -- build the progress callback, call
    compare(), merge in whatever read errors the walk phase(s) already
    produced, mark the job done -- has no mode-specific meaning: it is pure
    execution, identical whether the caller walked one folder or two. What
    differs between callers (how many folders were walked, what phases they
    reported) stays in each caller. comparer.py itself is never touched by
    this extraction.

    Mutates `job` in place (phase, processed, total, report, status).
    """
    job.phase = "comparing"
    job.processed = 0
    job.total = 0

    def compare_progress_cb(sp) -> None:
        job.processed = sp.buckets_done
        job.total = sp.buckets_total
        job.bytes_resolved = sp.bytes_resolved
        job.bytes_to_resolve = sp.bytes_to_resolve
        job.current_path = sp.current_path
        if isinstance(cache, PersistentHashCache):
            job.cache_hits = cache.cache_hits
            job.cache_misses = cache.cache_misses
        job.eta.observe(time.monotonic(), sp.bytes_resolved)

    report = compare(entries_a, entries_b, cache, progress=compare_progress_cb, io_workers=io_workers)
    report.errors = pre_errors + report.errors

    job.report = report
    job.phase = "done"
    job.status = "done"


def _run_scan_job(
    job_id: str, a: str, b: str, rules: dict[str, str], io_workers: int, use_cache: bool
) -> None:
    job = JOBS[job_id]
    cache: HashCache | None = None
    try:
        skip_abs_paths = {path for path, action in rules.items() if action in ("skip", "trash")}

        noisy_a = find_noisy_dirs(a, "A")
        trash_dirs = [nd for nd in noisy_a if rules.get(nd.abs_path) == "trash"]
        job.trash_dirs = trash_dirs

        def scan_progress_cb(files_found: int, current_path: str) -> None:
            job.processed = files_found
            job.current_path = current_path

        job.phase = "scanning_a"
        entries_a, errors_a = scan(a, skip_abs_paths, progress=scan_progress_cb)

        job.phase = "scanning_b"
        job.processed = 0
        entries_b, errors_b = scan(b, skip_abs_paths, progress=scan_progress_cb)

        cache = PersistentHashCache(_open_job_store()) if use_cache else HashCache()
        _run_compare_pipeline(job, entries_a, entries_b, errors_a + errors_b, cache, io_workers)
    except Exception as exc:  # the worker thread must never die silently
        print(f"scan job {job_id} failed: {exc}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        job.status = "error"
        job.error = str(exc)
    finally:
        # The single most important line for resumability: an aborted scan
        # must still keep everything it had already hashed. cache wraps this
        # job's own private Store (see _open_job_store()), so closing it here
        # only flushes and closes this job's connection -- never one shared
        # with another still-running job or with /api/settings, /api/cache/*.
        if cache is not None:
            cache.close()


def _run_dedupe_job(job_id: str, folder: str, rules: dict[str, str], io_workers: int, use_cache: bool) -> None:
    """Background worker for dedupe mode: one folder in, compare() against an empty B.

    Same shape as _run_scan_job but with one tree to walk instead of two, and
    no "scanning_b" phase. Both share _run_compare_pipeline for the tail.
    Reusing compare() with an empty entries_b list is deliberate -- see
    docs/superpowers/specs/2026-08-05-dedupe-mode-design.md section 2: the
    four-stage pipeline already classifies internal duplicates correctly
    with zero changes to comparer.py.
    """
    job = JOBS[job_id]
    cache: HashCache | None = None
    try:
        skip_abs_paths = {path for path, action in rules.items() if action in ("skip", "trash")}

        noisy = find_noisy_dirs(folder, "A")
        trash_dirs = [nd for nd in noisy if rules.get(nd.abs_path) == "trash"]
        job.trash_dirs = trash_dirs

        def scan_progress_cb(files_found: int, current_path: str) -> None:
            job.processed = files_found
            job.current_path = current_path

        job.phase = "scanning_a"
        entries, errors = scan(folder, skip_abs_paths, progress=scan_progress_cb)

        cache = PersistentHashCache(_open_job_store()) if use_cache else HashCache()
        _run_compare_pipeline(job, entries, [], errors, cache, io_workers)
    except Exception as exc:  # the worker thread must never die silently
        print(f"dedupe job {job_id} failed: {exc}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        job.status = "error"
        job.error = str(exc)
    finally:
        if cache is not None:
            cache.close()


# --------------------------------------------------------------------------
# API route handlers -- each returns (http_status, json_payload)
# --------------------------------------------------------------------------


def _drive_shortcuts() -> list[dict]:
    """List available drives: A:-Z: on native Windows, or mounted /mnt/<letter> on WSL/Linux."""
    if os.name == "nt":
        shortcuts = []
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            root = f"{letter}:\\"
            if os.path.exists(root):
                shortcuts.append({"name": f"Drive {letter}:", "path": root})
        return shortcuts

    shortcuts: list[dict] = []
    try:
        names = sorted(os.listdir("/mnt"))
    except OSError:
        return shortcuts

    for name in names:
        if len(name) != 1 or not name.isalpha():
            continue
        path = os.path.join("/mnt", name)
        try:
            if os.path.ismount(path):
                shortcuts.append({"name": f"Drive {name.upper()}:", "path": path})
        except OSError:
            continue

    return shortcuts


def handle_browse(body: dict) -> tuple[int, dict]:
    path = body.get("path") or ""

    if path == "":
        dirs = []
        home = os.path.expanduser("~")
        if os.path.isdir(home):
            dirs.append({"name": "Home", "path": home})
        dirs.extend(_drive_shortcuts())
        dirs.append({"name": "Filesystem root", "path": "/"})
        return 200, {"path": "", "parent": None, "dirs": dirs}

    if not os.path.isdir(path):
        return 400, {"error": f"not a directory: {path}"}

    try:
        names = sorted(os.listdir(path))
    except OSError as exc:
        return 403, {"error": f"cannot list directory: {exc}"}

    dirs = []
    for name in names:
        full = os.path.join(path, name)
        try:
            if os.path.isdir(full):
                dirs.append({"name": name, "path": full})
        except OSError:
            continue

    normalized = path.rstrip("/") or "/"
    parent = os.path.dirname(normalized)
    if parent == normalized:
        parent = None

    return 200, {"path": path, "parent": parent, "dirs": dirs}


def handle_prescan(body: dict) -> tuple[int, dict]:
    a = body.get("a", "")
    b = body.get("b") or None  # dedupe mode omits b entirely

    if b is None:
        if not os.path.isdir(a):
            return 400, {"error": f"folder does not exist or is not a directory: {a}"}
        a = os.path.realpath(a)
        with _single_flight(f"prescan:{a}"):
            noisy = find_noisy_dirs(a, "A")
        return 200, {"noisy": [asdict(n) for n in noisy]}

    try:
        validate_sources(a, b)
    except ValueError as exc:
        return 400, {"error": str(exc)}

    # realpath, not the raw input: on Windows this resolves through
    # _getfinalpathname, which returns the \\?\-prefixed extended-length form
    # for paths that exist. Everything downstream (os.walk, os.stat, open)
    # then bypasses MAX_PATH (260 chars) instead of failing with WinError 3 on
    # a deeply nested tree.
    a, b = os.path.realpath(a), os.path.realpath(b)

    # Walking both trees is expensive; refuse a duplicate rather than race it.
    with _single_flight(f"prescan:{a}|{b}"):
        noisy = find_noisy_dirs(a, "A") + find_noisy_dirs(b, "B")

    return 200, {"noisy": [asdict(n) for n in noisy]}


def handle_scan(body: dict) -> tuple[int, dict]:
    a = body.get("a", "")
    b = body.get("b", "")
    rules = body.get("rules") or {}
    io_workers = body.get("io_workers", 1)
    use_cache = body.get("use_cache", True)

    if not isinstance(io_workers, int) or isinstance(io_workers, bool) or not (1 <= io_workers <= 32):
        return 400, {"error": "io_workers must be an integer between 1 and 32"}

    try:
        validate_sources(a, b)
    except ValueError as exc:
        return 400, {"error": str(exc)}

    # See the matching comment in handle_prescan: this is what gives the scan
    # and hash stages long-path support on Windows.
    a, b = os.path.realpath(a), os.path.realpath(b)

    config = {"a": a, "b": b, "rules": rules, "io_workers": io_workers, "use_cache": use_cache}

    with JOBS_LOCK:
        # A repeated click must not spawn a second scanning thread over the same
        # tree. An identical request already in flight returns that same job, so
        # the caller simply keeps polling the scan that is already running.
        for existing in JOBS.values():
            if existing.status == "running" and existing.config == config:
                return 200, {"job_id": existing.id}

        job_id = uuid.uuid4().hex
        JOBS[job_id] = Job(id=job_id, config=config)

    thread = threading.Thread(
        target=_run_scan_job, args=(job_id, a, b, rules, io_workers, use_cache), daemon=True
    )
    thread.start()

    return 200, {"job_id": job_id}


def handle_dedupe_scan(body: dict) -> tuple[int, dict]:
    folder = body.get("folder", "")
    rules = body.get("rules") or {}
    io_workers = body.get("io_workers", 1)
    use_cache = body.get("use_cache", True)

    if not isinstance(io_workers, int) or isinstance(io_workers, bool) or not (1 <= io_workers <= 32):
        return 400, {"error": "io_workers must be an integer between 1 and 32"}

    if not os.path.isdir(folder):
        return 400, {"error": f"folder does not exist or is not a directory: {folder}"}

    # Same long-path rationale as handle_scan: see the comment there.
    folder = os.path.realpath(folder)

    config = {"folder": folder, "rules": rules, "io_workers": io_workers, "use_cache": use_cache}

    with JOBS_LOCK:
        for existing in JOBS.values():
            if existing.status == "running" and existing.mode == "dedupe" and existing.config == config:
                return 200, {"job_id": existing.id}

        job_id = uuid.uuid4().hex
        JOBS[job_id] = Job(id=job_id, mode="dedupe", config=config)

    thread = threading.Thread(
        target=_run_dedupe_job, args=(job_id, folder, rules, io_workers, use_cache), daemon=True
    )
    thread.start()

    return 200, {"job_id": job_id}


def handle_progress(job_id: str) -> tuple[int, dict]:
    job = JOBS.get(job_id)
    if job is None:
        return 404, {"error": f"unknown job: {job_id}"}

    now = time.monotonic()
    eta_seconds = None
    throughput_bps = None
    if job.phase == "comparing":
        eta_seconds = job.eta.seconds_remaining(now, job.bytes_to_resolve)
        throughput_bps = job.eta.throughput_bps(now)

    return 200, {
        "status": job.status,
        "phase": job.phase,
        "processed": job.processed,
        "total": job.total,
        "error": job.error,
        "bytes_resolved": job.bytes_resolved,
        "bytes_to_resolve": job.bytes_to_resolve,
        "throughput_bps": throughput_bps,
        "eta_seconds": eta_seconds,
        "elapsed_seconds": now - job.started_at,
        "current_path": job.current_path,
        "cache_hits": job.cache_hits,
        "cache_misses": job.cache_misses,
    }


def handle_report(job_id: str) -> tuple[int, dict]:
    job = JOBS.get(job_id)
    if job is None:
        return 404, {"error": f"unknown job: {job_id}"}
    if job.mode != "compare":
        return 409, {"error": f"job {job_id} is not a compare-mode job"}
    if job.status != "done":
        return 409, {"error": f"job is not finished yet (status={job.status})"}

    report = job.report
    return 200, {
        "rows": [asdict(row) for row in report.rows],
        "errors": [asdict(err) for err in report.errors],
        "stats": asdict(report.stats),
    }


def handle_dedupe_report(job_id: str) -> tuple[int, dict]:
    job = JOBS.get(job_id)
    if job is None:
        return 404, {"error": f"unknown job: {job_id}"}
    if job.mode != "dedupe":
        return 409, {"error": f"job {job_id} is not a dedupe-mode job"}
    if job.status != "done":
        return 409, {"error": f"job is not finished yet (status={job.status})"}

    groups, empty_group = group_duplicates(job.report.rows)

    def _group_payload(g):
        return {
            "digest": g.digest,
            "size": g.size,
            "wasted_bytes": g.wasted_bytes,
            "members": [asdict(m) for m in g.members],
        }

    return 200, {
        "groups": [_group_payload(g) for g in groups],
        "empty_group": _group_payload(empty_group) if empty_group is not None else None,
        "stats": asdict(job.report.stats),
        "errors": [asdict(err) for err in job.report.errors],
    }


def handle_dedupe_resolve(body: dict) -> tuple[int, dict]:
    job_id = body.get("job_id", "")
    keep_rules = body.get("keep_rules") or []

    if any(not isinstance(r, str) or r.strip("/") == "" for r in keep_rules):
        return 400, {"error": "keep_rules must be non-empty subtree paths"}

    job = JOBS.get(job_id)
    if job is None:
        return 404, {"error": f"unknown job: {job_id}"}
    if job.mode != "dedupe":
        return 409, {"error": f"job {job_id} is not a dedupe-mode job"}
    if job.status != "done":
        return 409, {"error": f"job is not finished yet (status={job.status})"}

    groups, _empty_group = group_duplicates(job.report.rows)
    kept = resolve(groups, keep_rules)

    return 200, {"kept": kept}


def handle_apply(body: dict) -> tuple[int, dict]:
    job_id = body.get("job_id", "")
    dest = body.get("dest", "")
    selected = body.get("selected") or []

    job = JOBS.get(job_id)
    if job is None:
        return 404, {"error": f"unknown job: {job_id}"}
    if job.mode != "compare":
        return 409, {"error": f"job {job_id} is not a compare-mode job"}
    if job.status != "done":
        return 409, {"error": f"job is not finished yet (status={job.status})"}

    a = job.config.get("a", "")
    b = job.config.get("b", "")

    try:
        validate_paths(a, b, dest)
    except ValueError as exc:
        return 400, {"error": str(exc)}

    # dest may not exist yet (see validate_paths' docstring): realpath still
    # resolves the \\?\ extended-length form on Windows by walking up to the
    # deepest existing ancestor, so long destination trees stay long-path safe.
    dest = os.path.realpath(dest)

    rows_by_id = {row.id: row for row in job.report.rows}
    entries: list[tuple[str, str]] = []
    for row_id in selected:
        row = rows_by_id.get(row_id)
        if row is None:
            return 400, {"error": f"unknown file id in selection: {row_id}"}
        if row.status in ("present_in_b", "unreadable"):
            return 400, {"error": f"cannot move a file with status '{row.status}': {row_id}"}
        entries.append((row.abs_path, row.rel_path))

    # Moving files is destructive and not idempotent: the second run of the same
    # apply would find its sources already gone. Claim the job atomically so a
    # double-click can never start a second pass over the same selection.
    with JOBS_LOCK:
        if job.applied:
            return 409, {
                "error": "this scan has already been applied; run a new scan to move more files"
            }
        if job.applying:
            return 409, {"error": "these files are already being moved; wait for it to finish"}
        job.applying = True

    try:
        os.makedirs(dest, exist_ok=True)

        cache = HashCache()
        move_result = apply_moves(entries, dest, cache)

        # One directory at a time, so a single failure (a folder locked by
        # Windows, say) is reported without discarding the ones that succeeded.
        for noisy_dir in job.trash_dirs:
            try:
                move_result.trashed.extend(move_to_trash([noisy_dir], dest))
            except (OSError, ValueError) as exc:
                move_result.errors.append({"path": noisy_dir.abs_path, "error": str(exc)})

        payload = {
            "config": job.config,
            "stats": asdict(job.report.stats),
            "moved": move_result.moved,
            "skipped_identical": move_result.skipped_identical,
            "renamed": move_result.renamed,
            "trashed": move_result.trashed,
            "errors": move_result.errors,
        }
        report_path = write_report(dest, payload)
        job.applied = True
    finally:
        job.applying = False

    return 200, {
        "moved": move_result.moved,
        "skipped_identical": move_result.skipped_identical,
        "renamed": move_result.renamed,
        "trashed": move_result.trashed,
        "errors": move_result.errors,
        "report_path": report_path,
    }


def handle_dedupe_apply(body: dict) -> tuple[int, dict]:
    job_id = body.get("job_id", "")
    job = JOBS.get(job_id)
    if job is None:
        return 404, {"error": f"unknown job: {job_id}"}
    if job.mode != "dedupe":
        return 409, {"error": f"job {job_id} is not a dedupe-mode job"}
    if job.status != "done":
        return 409, {"error": f"job is not finished yet (status={job.status})"}
    return 501, {"error": "not implemented yet"}


DEFAULT_SETTINGS = {"last_paths": None, "io_workers": 1, "use_cache": True}
SETTINGS_KEYS = set(DEFAULT_SETTINGS)


def handle_settings_get() -> tuple[int, dict]:
    store = _get_store()
    result = dict(DEFAULT_SETTINGS)
    for key in SETTINGS_KEYS:
        value = store.get_setting(key)
        if value is not None:
            result[key] = value
    return 200, result


def handle_settings_post(body: dict) -> tuple[int, dict]:
    unknown = set(body) - SETTINGS_KEYS
    if unknown:
        return 400, {"error": f"unknown settings key(s): {', '.join(sorted(unknown))}"}
    store = _get_store()
    for key, value in body.items():
        store.set_setting(key, value)
    return 200, {}


def handle_volumes(body: dict) -> tuple[int, dict]:
    paths = [p for p in (body.get("a"), body.get("b")) if p]
    if not paths:
        return 400, {"error": "at least one path is required"}
    volumes = [detect(p) for p in paths]
    return 200, {"volumes": [asdict(v) for v in volumes], "suggested_workers": combine(volumes)}


def handle_cache_stats() -> tuple[int, dict]:
    store = _get_store()
    return 200, {"row_count": store.row_count(), "db_size_bytes": store.db_size_bytes()}


def handle_cache_clear(body: dict) -> tuple[int, dict]:
    _get_store().clear_hashes()
    return 200, {}


POST_ROUTES = {
    "/api/browse": handle_browse,
    "/api/prescan": handle_prescan,
    "/api/scan": handle_scan,
    "/api/dedupe/scan": handle_dedupe_scan,
    "/api/dedupe/resolve": handle_dedupe_resolve,
    "/api/apply": handle_apply,
    "/api/settings": handle_settings_post,
    "/api/volumes": handle_volumes,
    "/api/cache/clear": handle_cache_clear,
}


# --------------------------------------------------------------------------
# HTTP handler
# --------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "DupefinderHTTP/0.1"

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass  # keep stdout clean

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)

        if parsed.path == "/":
            self._serve_static("index.html", "text/html; charset=utf-8")
        elif parsed.path == "/app.js":
            self._serve_static("app.js", "application/javascript; charset=utf-8")
        elif parsed.path == "/style.css":
            self._serve_static("style.css", "text/css; charset=utf-8")
        elif parsed.path == "/api/progress":
            job_id = parse_qs(parsed.query).get("job", [""])[0]
            self._send_json(*handle_progress(job_id))
        elif parsed.path == "/api/report":
            job_id = parse_qs(parsed.query).get("job", [""])[0]
            self._send_json(*handle_report(job_id))
        elif parsed.path == "/api/dedupe/report":
            job_id = parse_qs(parsed.query).get("job", [""])[0]
            self._send_json(*handle_dedupe_report(job_id))
        elif parsed.path == "/api/settings":
            self._send_json(*handle_settings_get())
        elif parsed.path == "/api/cache/stats":
            self._send_json(*handle_cache_stats())
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)

        handler = POST_ROUTES.get(parsed.path)
        if handler is None:
            self._send_json(404, {"error": "not found"})
            return

        try:
            body = self._read_json_body()
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return

        try:
            status, payload = handler(body)
        except Busy as exc:
            status, payload = 409, {"error": str(exc)}
        except Exception as exc:  # never leak a raw traceback to the client
            print(f"unhandled error in {parsed.path}: {exc}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            status, payload = 500, {"error": "internal server error"}

        self._send_json(status, payload)

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON body: {exc}") from exc

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self, filename: str, content_type: str) -> None:
        real_web_dir = os.path.realpath(WEB_DIR)
        real_file_path = os.path.realpath(os.path.join(WEB_DIR, filename))

        if os.path.commonpath([real_file_path, real_web_dir]) != real_web_dir:
            self._send_json(404, {"error": "not found"})
            return

        try:
            with open(real_file_path, "rb") as f:
                body = f.read()
        except OSError:
            self._send_json(404, {"error": "not found"})
            return

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def _open_browser(url: str) -> None:
    """Open `url` in the default browser, never failing the server if it can't.

    webbrowser is deliberately not used first on WSL: it would try to launch
    a Linux browser that usually isn't installed there.
    """
    if is_wsl():
        for cmd in (["wslview", url], ["explorer.exe", url]):
            try:
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
            except OSError:
                continue
        print(f"Could not open a browser automatically. Open {url} manually.")
        return

    if sys.platform == "darwin" or os.name == "nt":
        if webbrowser.open(url):
            return
        print(f"Could not open a browser automatically. Open {url} manually.")
        return

    # Native Linux
    try:
        subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    except OSError:
        pass
    if webbrowser.open(url):
        return
    print(f"Could not open a browser automatically. Open {url} manually.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Local web UI for comparing two folders by content."
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument(
        "--cache-dir",
        default=None,
        help="override the persistent hash cache location (mainly for tests)",
    )
    args = parser.parse_args()

    global _CACHE_DIR_OVERRIDE
    _CACHE_DIR_OVERRIDE = args.cache_dir

    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError:
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)

    port = httpd.server_address[1]
    url = f"http://127.0.0.1:{port}/"
    print(f"dupefinder running at {url}")

    if not args.no_browser:
        _open_browser(url)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        if _STORE is not None:
            _STORE.close()


if __name__ == "__main__":
    main()
