"""ThreadingHTTPServer exposing the JSON API and serving the static web UI."""

import argparse
import json
import os
import subprocess
import sys
import threading
import traceback
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from dupefinder.comparer import compare
from dupefinder.hashing import HashCache
from dupefinder.models import NoisyDir, Report
from dupefinder.mover import (
    apply_moves,
    move_to_trash,
    validate_paths,
    validate_sources,
    write_report,
)
from dupefinder.scanner import find_noisy_dirs, scan

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
DEFAULT_PORT = 8765


@dataclass
class Job:
    id: str
    status: str = "running"  # "running" | "done" | "error"
    phase: str = "scanning_a"  # "scanning_a" | "scanning_b" | "comparing" | "done"
    processed: int = 0
    total: int = 0  # 0 means indeterminate (scanning phases)
    report: Report | None = None
    error: str | None = None
    config: dict = field(default_factory=dict)  # {a, b, rules}
    trash_dirs: list[NoisyDir] = field(default_factory=list)  # internal: resolved at scan time
    applying: bool = False  # an apply is running right now
    applied: bool = False  # an apply already completed; never run a second one


JOBS: dict[str, Job] = {}
JOBS_LOCK = threading.Lock()


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


def _run_scan_job(job_id: str, a: str, b: str, rules: dict[str, str]) -> None:
    job = JOBS[job_id]
    try:
        skip_abs_paths = {path for path, action in rules.items() if action in ("skip", "trash")}

        noisy_a = find_noisy_dirs(a, "A")
        trash_dirs = [nd for nd in noisy_a if rules.get(nd.abs_path) == "trash"]
        job.trash_dirs = trash_dirs

        job.phase = "scanning_a"
        entries_a, errors_a = scan(a, skip_abs_paths)

        job.phase = "scanning_b"
        entries_b, errors_b = scan(b, skip_abs_paths)

        job.phase = "comparing"

        def progress_cb(processed: int, total: int) -> None:
            job.processed = processed
            job.total = total

        cache = HashCache()
        report = compare(entries_a, entries_b, cache, progress=progress_cb)
        report.errors = errors_a + errors_b + report.errors

        job.report = report
        job.phase = "done"
        job.status = "done"
    except Exception as exc:  # the worker thread must never die silently
        print(f"scan job {job_id} failed: {exc}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        job.status = "error"
        job.error = str(exc)


# --------------------------------------------------------------------------
# API route handlers -- each returns (http_status, json_payload)
# --------------------------------------------------------------------------


def _drive_shortcuts() -> list[dict]:
    """List Windows drives currently mounted under /mnt (WSL).

    os.path.ismount() filters out stale mount points -- an empty /mnt/d left
    behind by a previous session is a directory, but not a mounted drive.
    Requiring a single-letter name excludes WSL's own /mnt/wsl and /mnt/wslg.
    Returns an empty list on any error, so the picker still works off WSL.
    """
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
    b = body.get("b", "")

    try:
        validate_sources(a, b)
    except ValueError as exc:
        return 400, {"error": str(exc)}

    # Walking both trees is expensive; refuse a duplicate rather than race it.
    with _single_flight(f"prescan:{a}|{b}"):
        noisy = find_noisy_dirs(a, "A") + find_noisy_dirs(b, "B")

    return 200, {"noisy": [asdict(n) for n in noisy]}


def handle_scan(body: dict) -> tuple[int, dict]:
    a = body.get("a", "")
    b = body.get("b", "")
    rules = body.get("rules") or {}

    try:
        validate_sources(a, b)
    except ValueError as exc:
        return 400, {"error": str(exc)}

    config = {"a": a, "b": b, "rules": rules}

    with JOBS_LOCK:
        # A repeated click must not spawn a second scanning thread over the same
        # tree. An identical request already in flight returns that same job, so
        # the caller simply keeps polling the scan that is already running.
        for existing in JOBS.values():
            if existing.status == "running" and existing.config == config:
                return 200, {"job_id": existing.id}

        job_id = uuid.uuid4().hex
        JOBS[job_id] = Job(id=job_id, config=config)

    thread = threading.Thread(target=_run_scan_job, args=(job_id, a, b, rules), daemon=True)
    thread.start()

    return 200, {"job_id": job_id}


def handle_progress(job_id: str) -> tuple[int, dict]:
    job = JOBS.get(job_id)
    if job is None:
        return 404, {"error": f"unknown job: {job_id}"}
    return 200, {
        "status": job.status,
        "phase": job.phase,
        "processed": job.processed,
        "total": job.total,
        "error": job.error,
    }


def handle_report(job_id: str) -> tuple[int, dict]:
    job = JOBS.get(job_id)
    if job is None:
        return 404, {"error": f"unknown job: {job_id}"}
    if job.status != "done":
        return 409, {"error": f"job is not finished yet (status={job.status})"}

    report = job.report
    return 200, {
        "rows": [asdict(row) for row in report.rows],
        "errors": [asdict(err) for err in report.errors],
        "stats": asdict(report.stats),
    }


def handle_apply(body: dict) -> tuple[int, dict]:
    job_id = body.get("job_id", "")
    dest = body.get("dest", "")
    selected = body.get("selected") or []

    job = JOBS.get(job_id)
    if job is None:
        return 404, {"error": f"unknown job: {job_id}"}
    if job.status != "done":
        return 409, {"error": f"job is not finished yet (status={job.status})"}

    a = job.config.get("a", "")
    b = job.config.get("b", "")

    try:
        validate_paths(a, b, dest)
    except ValueError as exc:
        return 400, {"error": str(exc)}

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


POST_ROUTES = {
    "/api/browse": handle_browse,
    "/api/prescan": handle_prescan,
    "/api/scan": handle_scan,
    "/api/apply": handle_apply,
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
    for cmd in (["wslview", url], ["xdg-open", url], ["explorer.exe", url]):
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except OSError:
            continue
    print(f"Could not open a browser automatically. Open {url} manually.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Local web UI for comparing two folders by content."
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

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


if __name__ == "__main__":
    main()
