# Implementation Spec — `files-duplicated`

> **This document is an executable specification.** It is written so an implementing model
> can build the project end to end without making design decisions. Every ambiguity that
> came up during design has already been resolved below. If something seems unspecified,
> prefer the simplest option consistent with the rest of this document and note it in the
> CHANGELOG — do not invent new features.

---

## 1. Context

Gabriel has files scattered across two folders and needs to answer: *"which files do I have
in A that I do not have in B?"* — then move exactly those into a third folder.

Comparing by **filename does not work**: the same file may have been renamed, and two files
with the same name may hold different content. Identity must be determined by **content
hash**, never by name, timestamp, or path.

The deliverable is a local web UI: pick three folders, review a report of A-exclusive files,
tick which ones to move, then apply. Nothing on disk is modified until the user presses the
apply button.

`/home/gabriel/_projects/propios/files-duplicated` is currently **empty**. This is a
greenfield build.

---

## 2. Hard Rules

These are non-negotiable. Violating any of them is a bug.

1. **Folder B is never modified.** Not moved from, not deleted from, not written to. Ever.
2. **Nothing on disk changes until `POST /api/apply` is called.** Scanning and reporting are
   strictly read-only.
3. **Nothing is ever permanently deleted.** The "delete" affordance moves directories into
   `<DEST>/_trash/` instead.
4. **All code, comments, docstrings, UI strings, README and CHANGELOG are in English.**
5. **Zero third-party dependencies.** Python 3.12 standard library only. No `pip install`,
   no `npm install`, no CDN links in the HTML.
6. **Symbolic links are never followed** (neither directories nor files). Prevents infinite
   loops and duplicate accounting.
7. **A read error never aborts a scan.** It is recorded and the scan continues.

---

## 3. Environment

- Python 3.12.3 at `/usr/bin/python3`. `tkinter` is **not** installed — do not use it.
- WSL2. Destination may well be on a different filesystem than the source
  (`/home` vs `/mnt/c`), so use `shutil.move`, never `os.rename`, for the final moves.
- Browser opening: try `wslview`, then `xdg-open`, then `explorer.exe`; if all fail, just
  print the URL. Never fail the server because a browser could not be opened.

---

## 4. Directory Layout

Create exactly this structure:

```
files-duplicated/
├── IMPLEMENTATION_SPEC.md      # this document — already present, do not modify
├── README.md
├── CHANGELOG.md
├── run.sh                      # executable (chmod +x)
├── dupefinder/
│   ├── __init__.py             # __version__ = "0.1.0"
│   ├── models.py               # dataclasses shared by every module
│   ├── hashing.py              # partial/full SHA-256 + cache
│   ├── scanner.py              # directory walking + noisy-dir detection
│   ├── comparer.py             # the 3-stage comparison pipeline
│   ├── mover.py                # path validation, moves, trash, JSON report
│   └── server.py               # ThreadingHTTPServer + JSON API + static files
├── web/
│   ├── index.html
│   ├── app.js
│   └── style.css
└── tests/
    ├── __init__.py
    ├── helpers.py              # temp-tree fixture builder
    ├── test_hashing.py
    ├── test_scanner.py
    ├── test_comparer.py
    └── test_mover.py
```

---

## 5. `dupefinder/models.py`

Use `dataclasses`. `FileEntry` is frozen; the rest are mutable.

```python
@dataclass(frozen=True)
class FileEntry:
    abs_path: str    # absolute, realpath-resolved
    rel_path: str    # relative to its scan root, always POSIX "/" separators
    size: int        # bytes

@dataclass
class ScanError:
    path: str
    error: str       # str(exception)

@dataclass
class NoisyDir:
    abs_path: str
    rel_path: str
    root: str        # "A" or "B"
    reason: str      # "vcs" | "dependencies" | "cache" | "system" | "hidden"
    file_count: int
    total_bytes: int
    counts_truncated: bool = False   # counts stopped at COUNT_LIMIT; treat them as a floor

@dataclass
class ReportRow:
    id: str                      # equals rel_path; stable identifier used by the UI
    abs_path: str
    rel_path: str
    size: int
    status: str                  # see status vocabulary below
    sha256: str | None           # None when the file was never hashed
    duplicate_of: str | None     # rel_path of the representative; only for internal_copy

@dataclass
class Stats:
    files_a: int = 0
    files_b: int = 0
    bytes_a: int = 0
    bytes_b: int = 0
    partial_hashes: int = 0
    full_hashes: int = 0
    bytes_read: int = 0
    elapsed_seconds: float = 0.0

@dataclass
class Report:
    rows: list[ReportRow]
    errors: list[ScanError]
    stats: Stats
```

### Status vocabulary (exact strings — the UI depends on them)

| `status` | Meaning | Ticked by default in UI |
|---|---|---|
| `exclusive` | Content not present in B; first representative of its hash within A | **yes** |
| `internal_copy` | Content not present in B, but another A file already represents this hash | no |
| `present_in_b` | Content exists in B | no (hidden from the main table) |
| `unreadable` | The A file could not be read; classification impossible | no |

---

## 6. `dupefinder/hashing.py`

```python
CHUNK_SIZE = 1024 * 1024      # 1 MiB read buffer
PARTIAL_SIZE = 64 * 1024      # 64 KiB prefix used by the cheap pre-filter

class ReadError(Exception):
    """Raised when a file cannot be read. Carries the offending path."""
    def __init__(self, path: str, cause: Exception) -> None: ...
    path: str

def partial_hash(path: str) -> str:
    """SHA-256 hex digest of the first PARTIAL_SIZE bytes. Raises ReadError."""

def full_hash(path: str) -> str:
    """SHA-256 hex digest of the whole file, read in CHUNK_SIZE blocks.
    Never loads the file into memory at once. Raises ReadError."""

class HashCache:
    """Memoizes digests per absolute path for the lifetime of one scan job.
    Also accumulates the statistics reported to the UI."""
    def partial(self, path: str) -> str: ...
    def full(self, path: str) -> str: ...
    partial_calls: int
    full_calls: int
    bytes_read: int
```

Notes for the implementer:

- For a file of `size <= PARTIAL_SIZE`, the partial and full digests are identical by
  construction. This is harmless: partial digests are only ever compared against other
  partial digests, and full against full. Do not add special-case logic.
- `bytes_read` accumulates the bytes actually read from disk, so the UI can show how much
  I/O the pre-filter avoided.
- Wrap every `OSError` (permissions, I/O, disappeared file) into `ReadError`.

---

## 7. `dupefinder/scanner.py`

```python
NOISY_PATTERNS: dict[str, str] = {
    ".git": "vcs", ".svn": "vcs", ".hg": "vcs",
    "node_modules": "dependencies", "vendor": "dependencies", ".venv": "dependencies",
    "venv": "dependencies", "__pycache__": "cache", ".cache": "cache",
    ".pytest_cache": "cache", ".mypy_cache": "cache",
    "$RECYCLE.BIN": "system", "System Volume Information": "system",
    ".Trash-1000": "system",
}

def find_noisy_dirs(root: str, root_label: str) -> list[NoisyDir]:
    """Top-down walk. When a directory name matches NOISY_PATTERNS, or starts with '.'
    and is not the root itself, record it as a NoisyDir with its recursive file count and
    byte total, then PRUNE it (do not descend). Pruning prevents reporting a .git nested
    inside a node_modules as a separate entry.
    Directories that match a NOISY_PATTERNS key take that key's reason; other dot-directories
    take reason "hidden".
    Symlinked directories are skipped entirely and never reported.
    The recursive count stops after COUNT_LIMIT (2000) files and sets
    NoisyDir.counts_truncated. The number is only there to inform the user's
    compare/skip/trash choice, and an external NTFS drive's $RECYCLE.BIN would
    otherwise stall the pre-scan when walked in full over WSL's 9p mount."""

def scan(root: str, skip_abs_paths: set[str]) -> tuple[list[FileEntry], list[ScanError]]:
    """os.walk(root, followlinks=False), pruning dirnames in place when the joined absolute
    path is in skip_abs_paths.
    Skips any entry where os.path.islink() is true.
    rel_path = os.path.relpath(abs_path, root).replace(os.sep, "/").
    A failing os.stat() produces a ScanError and the file is omitted from the entry list.
    The returned list is sorted by rel_path — determinism matters for representative
    selection in the comparer."""
```

---

## 8. `dupefinder/comparer.py` — the core

```python
def compare(
    entries_a: list[FileEntry],
    entries_b: list[FileEntry],
    cache: HashCache,
    progress: Callable[[int, int], None] | None = None,
) -> Report
```

### Why three stages

Hashing everything is prohibitively expensive. The pipeline discards candidates as cheaply
as possible and only reads bytes when it must:

1. **Size buckets.** Two files with different sizes can never be equal. If a size appears
   exactly once across A and not at all in B, that A file is exclusive and unique — decided
   with **zero bytes read**.
2. **Partial hash** (first 64 KiB) on the size buckets that remain.
3. **Full SHA-256** only on files that survive stage 2.

On typical photo/document trees this avoids reading well over 90% of the bytes.

### Exact algorithm

```
1. by_size_a: dict[int, list[FileEntry]]  # built from entries_a
   by_size_b: dict[int, list[FileEntry]]  # built from entries_b
   Within every bucket, entries stay sorted by rel_path.

2. rows: list[ReportRow] = []
   b_full_hashes: set[str] = set()
   a_representative: dict[str, str] = {}   # full_hash -> rel_path of representative

3. FAST PATH — for each (size, group_a) in by_size_a:
       if len(group_a) == 1 and size not in by_size_b:
           emit ReportRow(status="exclusive", sha256=None)
           continue to next size

4. SLOW PATH — for the remaining sizes, with group_b = by_size_b.get(size, []):

   4a. Compute the partial hash of every file in group_a and group_b.
       - A file that raises ReadError:
           * if it belongs to A -> ReportRow(status="unreadable", sha256=None)
                                   + ScanError; drop it from further processing
           * if it belongs to B -> ScanError only; drop it from further processing
       - Group all surviving files into buckets keyed by their partial digest.

   4b. For each partial bucket:
       - If the bucket holds exactly one file and that file is from A, it is unique within
         this size: emit status="exclusive" with sha256=None. No full hash needed.
       - Otherwise compute the full hash of every file in the bucket, applying the same
         ReadError handling as 4a, and group by full digest.

   4c. Add every B full digest in this size to b_full_hashes.

   4d. For each A file that now has a full digest h, in rel_path order:
       - if h in b_full_hashes                 -> status="present_in_b"
       - elif h not in a_representative        -> status="exclusive";
                                                  a_representative[h] = rel_path
       - else                                  -> status="internal_copy",
                                                  duplicate_of = a_representative[h]

5. Sort rows by rel_path. Populate Stats. Return the Report.
```

### Decisions already made — do not revisit

- **Unreadable B file.** It is dropped, so an A file that is actually its duplicate will be
  classified `exclusive` and may get moved. This is the safe direction: B is untouched and
  the A file merely relocates to DEST, fully recoverable. Do not attempt to be clever here.
- **Empty files (size 0).** They all share one digest and fall out of the generic bucket
  logic correctly. No special case. There *will* be a test for this.
- **Representative selection** for `internal_copy` is the lexicographically smallest
  `rel_path`, which is why the scanner sorts. This must be deterministic across runs.
- **`progress`** is called as `progress(processed_buckets, total_buckets)` at the top of
  each size bucket in the slow path. Fast-path buckets count toward `processed` too.

---

## 9. `dupefinder/mover.py`

```python
@dataclass
class MoveResult:
    moved: list[dict]              # {rel_path, dest_path, size}
    skipped_identical: list[dict]  # {rel_path, dest_path} — source left untouched in A
    renamed: list[dict]            # {rel_path, dest_path, original_name}
    trashed: list[dict]            # {rel_path, dest_path}
    errors: list[dict]             # {path, error}

def validate_paths(a: str, b: str, dest: str) -> None:
    """Raises ValueError with a user-facing English message when any rule fails:
      - a and b must exist and be directories
      - dest must not equal a or b
      - dest must not live inside a or inside b
      - a must not live inside b, and b must not live inside a
    Containment is checked on os.path.realpath with os.path.commonpath.
    dest is allowed not to exist yet; the caller creates it."""

def apply_moves(entries: list[tuple[str, str]], dest: str, cache: HashCache) -> MoveResult:
    """entries is a list of (abs_path, rel_path) selected by the user.
    For each entry:
      target = os.path.join(dest, *rel_path.split("/"))
      os.makedirs(os.path.dirname(target), exist_ok=True)
      if target does not exist:
          shutil.move(abs_path, target)            -> moved
      elif full_hash(target) == full_hash(abs_path):
          leave the source in place                -> skipped_identical
      else:
          probe "{stem}_1{suffix}", "{stem}_2{suffix}", ... for the first free name,
          shutil.move there                        -> renamed
    Any OSError is captured into errors and the loop continues."""

def move_to_trash(noisy_dirs: list[NoisyDir], dest: str) -> list[dict]:
    """Only accepts NoisyDir entries whose root == "A". Passing a "B" entry is a programming
    error -> raise ValueError.
    Moves each directory to os.path.join(dest, "_trash", *rel_path.split("/")),
    creating parents. Returns the trashed list."""

def write_report(dest: str, payload: dict) -> str:
    """Writes <dest>/_report_<YYYYMMDD-HHMMSS>.json (UTF-8, indent=2, ensure_ascii=False)
    and returns the absolute path. payload contains the full MoveResult plus the job config
    and the Stats, so the run is auditable after the fact."""
```

Ordering inside `POST /api/apply`: **validate → create dest → move files → move trash →
write report**. Trash last so that a validation or move failure leaves the noisy directories
where they are.

---

## 10. `dupefinder/server.py`

`ThreadingHTTPServer` bound to `127.0.0.1`. Try port `8765`; on `OSError` bind port `0` and
use whatever the OS assigns. Print the final URL to stdout.

Static files: `GET /` serves `web/index.html`; `GET /app.js` and `GET /style.css` serve from
`web/` with the right `Content-Type`. Never serve a path outside `web/`.

Job state lives in a module-level `dict[str, Job]` guarded by a `threading.Lock`.

```python
@dataclass
class Job:
    id: str                 # uuid4 hex
    status: str             # "running" | "done" | "error"
    phase: str              # "scanning_a" | "scanning_b" | "comparing" | "done"
    processed: int
    total: int              # 0 means indeterminate (scanning phases)
    report: Report | None
    error: str | None
    config: dict            # {a, b, rules}
```

Every response is JSON with `Content-Type: application/json`. Errors return the appropriate
status code and `{"error": "<english message>"}`. Never leak a raw traceback to the client;
log it to stderr instead.

### Concurrency

The UI disables a button for the duration of its request, but the server must not rely on
that -- a double-click, a stale tab or a retry can always produce overlapping calls.

- `_single_flight(key)` is a context manager that admits one operation per key and raises
  `Busy` on a duplicate; `do_POST` maps `Busy` to **409**. `/api/prescan` uses it, keyed by
  `a|b`, because walking both trees is expensive.
- `/api/scan` is idempotent: under `JOBS_LOCK` it returns the id of a still-running job whose
  `config` is identical, instead of starting a second scanning thread. A differing `rules`
  dict counts as a different request and starts a new job.
- `/api/apply` carries `Job.applying` and `Job.applied`, both claimed atomically under
  `JOBS_LOCK` before any file is touched. Moving is destructive and not idempotent, so a
  second apply is refused with **409** -- while in progress and permanently afterwards.
  `applying` is cleared in a `finally` block so a failed apply can be retried.

### API contracts

**`POST /api/browse`** — powers the server-side folder picker. Browsers cannot expose
absolute paths, so navigation happens here.

```jsonc
// request — an empty/missing path returns the shortcut roots
{"path": "/home/gabriel"}
// response
{"path": "/home/gabriel",
 "parent": "/home",
 "dirs": [{"name": "_projects", "path": "/home/gabriel/_projects"}]}
// when path is empty:
{"path": "", "parent": null,
 "dirs": [{"name": "Home", "path": "/home/gabriel"},
          {"name": "Drive C:", "path": "/mnt/c"},
          {"name": "Drive D:", "path": "/mnt/d"},
          {"name": "Filesystem root", "path": "/"}]}
```

The drive shortcuts are discovered at request time by `_drive_shortcuts()`: entries under
`/mnt` whose name is a single letter **and** for which `os.path.ismount()` is true. The mount
check matters -- an empty `/mnt/d` left behind by a previous session is a directory but not a
mounted drive. The single-letter rule excludes WSL's own `/mnt/wsl` and `/mnt/wslg`.

Unreadable directories return `403` with an error message; the UI shows it inline and stays
on the previous directory. Hidden directories **are** listed here (the user may legitimately
want to pick one).

**`POST /api/prescan`**

```jsonc
{"a": "/path/a", "b": "/path/b"}
->
{"noisy": [{"abs_path": "...", "rel_path": ".git", "root": "A",
            "reason": "vcs", "file_count": 1204, "total_bytes": 51234567}]}
```

Runs `validate_paths` first (with `dest` omitted — add a `validate_sources(a, b)` helper for
this) and returns `400` on failure.

**`POST /api/scan`**

```jsonc
{"a": "...", "b": "...",
 "rules": {"/abs/path/of/noisy/dir": "compare" | "skip" | "trash"}}
->
{"job_id": "…"}
```

`skip` and `trash` both add the path to `skip_abs_paths` for the scan. `trash` additionally
records the directory for the apply step. Any noisy directory absent from `rules` defaults
to `compare`. Runs in a `threading.Thread`; returns immediately.

**`GET /api/progress?job=<id>`**

```jsonc
{"status": "running", "phase": "comparing", "processed": 812, "total": 2455, "error": null}
```

**`GET /api/report?job=<id>`** — `409` if the job is not `done`.

```jsonc
{"rows": [ReportRow, …],       // present_in_b rows included; the UI filters them
 "errors": [ScanError, …],
 "stats": Stats}
```

**`POST /api/apply`**

```jsonc
{"job_id": "…", "dest": "/path/dest", "selected": ["rel/path/one.jpg", …]}
->
{"moved": [...], "skipped_identical": [...], "renamed": [...],
 "trashed": [...], "errors": [...], "report_path": "/path/dest/_report_20260804-153000.json"}
```

Validates that `dest` is legal against the job's `a` and `b`, resolves each selected `id`
back to its `ReportRow`, and rejects (`400`) any id not present in the report or whose status
is `present_in_b` or `unreadable`.

---

## 11. `web/` — the UI

Vanilla HTML/CSS/JS. No framework, no build step, no external requests. One page with four
`<section>` elements; only the one carrying `.active` is visible.

**Screen 1 — `#screen-select`.** Three rows (Folder A / Folder B / Destination), each with a
text input holding the current path plus a *Browse* button that opens a modal driven by
`/api/browse`. The path can also be typed or pasted directly. *Continue* is disabled until
all three are non-empty; it calls `/api/prescan`.

**Screen 2 — `#screen-prescan`.** A table of the noisy directories: path, which root it came
from, file count, human-readable size, and a three-way radio group per row —
**Compare / Skip / Move to trash**. `.git`-style matches default to *Skip*.
**Rows whose `root` is `"B"` must not render the "Move to trash" radio at all** — folder B is
never modified. A "set all to…" control applies one choice to every row. *Start scan* calls
`/api/scan`, then polls `/api/progress` every 400 ms and drives a progress bar. If no noisy
directories were found, skip this screen entirely.

**Screen 3 — `#screen-report`.** The main table: checkbox, relative path, human size, status
badge, and for `internal_copy` a "copy of <path>" note. Rows with status `present_in_b` are
excluded from this table; show their count in the summary line instead. Features: sortable
columns (path, size, status), a text filter on path, status filter checkboxes, *Select all /
none*, and a live footer reading `N files selected, X.X GB`. `exclusive` rows start ticked;
`internal_copy` and `unreadable` start unticked. A collapsible panel lists read errors.
*Move selected files* asks for a confirmation that states the exact file count and total size
before calling `/api/apply`.

**Screen 4 — `#screen-result`.** Counts for moved / skipped as identical / renamed on
collision / moved to trash / errors, the path of the written JSON report, and a *Start over*
button that resets to screen 1.

Formatting helpers in `app.js`: `formatBytes(n)` (B/KB/MB/GB, one decimal) and
`escapeHtml(s)` — every path rendered into the DOM must go through `escapeHtml`, since
filenames can contain `<` and `&`.

---

## 12. `run.sh`

```sh
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
exec python3 -m dupefinder.server "$@"
```

`server.py` handles the browser-opening attempts described in section 3, under
`if __name__ == "__main__":`. Support `--port N` and `--no-browser` flags via `argparse`.

---

## 13. Tests — `python3 -m unittest discover tests -v`

`stdlib unittest` only. `tests/helpers.py` exposes
`build_tree(root: str, spec: dict[str, bytes])` which materialises nested files from a dict
of `{"rel/path": b"content"}`, plus a `TempTreeCase` base class wiring
`tempfile.TemporaryDirectory` into `setUp`/`tearDown`.

Required cases, by module:

**`test_hashing.py`**
1. `full_hash` matches a known SHA-256 vector.
2. Files larger than `CHUNK_SIZE` hash correctly (verifies chunked reads).
3. `partial_hash` of two files sharing their first 64 KiB but differing later is equal —
   this is what makes stage 3 necessary.
4. `HashCache` computes a digest once and serves repeats from cache (`full_calls == 1`).
5. An unreadable file raises `ReadError` carrying the path.

**`test_scanner.py`**
6. Nested tree returns correct POSIX `rel_path` values, sorted.
7. `skip_abs_paths` prunes a directory and everything under it.
8. A symlinked directory is neither descended into nor reported.
9. `find_noisy_dirs` reports `.git` with the right `reason`, `file_count` and `total_bytes`,
   and does not report a `.git` nested inside an already-reported `node_modules`.

**`test_comparer.py`**
10. Same content, different filename → `present_in_b`. *(The headline requirement.)*
11. Same filename, different content → `exclusive`.
12. Same size, different content → `exclusive` (guards against a false positive from the
    partial-hash stage).
13. Several empty files across A and B → classified correctly, no crash.
14. Three identical A files absent from B → exactly one `exclusive`, two `internal_copy`
    both pointing at the lexicographically smallest `rel_path`.
15. A file whose size does not occur in B at all → `exclusive` with `sha256 is None` and
    `cache.full_calls == 0`, proving the fast path fired.
16. Unreadable A file → status `unreadable` plus one entry in `Report.errors`; other files
    are still classified.

**`test_mover.py`**
17. Nested relative path is recreated under the destination.
18. Destination collision with identical content → `skipped_identical`, source still in A.
19. Destination collision with different content → moved as `name_1.ext`.
20. `validate_paths` rejects dest inside A, dest inside B, dest equal to A, and A inside B.
21. `move_to_trash` relocates a directory under `<dest>/_trash/` preserving its structure,
    and raises `ValueError` for a `root == "B"` entry.
22. `write_report` produces valid, re-readable JSON at the expected path.

---

## 14. `README.md`

English. Sections: what the tool does and why hashing beats filename matching; requirements
(Python 3.12, no dependencies); quick start (`./run.sh` → open the printed URL); a walkthrough
of the four screens; a short "How it works" explaining the three-stage pipeline; a **Safety**
section spelling out the four hard guarantees (B untouched, read-only until apply, nothing
permanently deleted, JSON audit report per run); and how to run the tests.

## 15. `CHANGELOG.md`

[Keep a Changelog](https://keepachangelog.com/) format, single entry:

```markdown
## [0.1.0] - 2026-08-04

### Added
- Content-based folder comparison using SHA-256, independent of filenames.
- Three-stage pipeline (size bucket → 64 KiB partial hash → full hash) to minimise disk reads.
- Local web UI: folder selection, noisy-directory pre-scan, sortable/filterable report with
  per-file selection, and a results summary.
- Internal-duplicate detection within folder A: only one representative is offered for moving.
- Relative directory structure preserved in the destination folder.
- Collision handling on move: identical files are skipped, differing files are suffixed.
- `_trash/` quarantine instead of permanent deletion.
- Per-run JSON audit report written to the destination folder.
```

---

## 16. Build Order

Follow this sequence. Run the tests for each module before moving on.

0. `IMPLEMENTATION_SPEC.md` (this file) already exists in the project root. Leave it as is.
1. `models.py`, `hashing.py` → `test_hashing.py` green
2. `scanner.py` → `test_scanner.py` green
3. `comparer.py` → `test_comparer.py` green ← **the highest-risk module; do not rush it**
4. `mover.py` → `test_mover.py` green
5. `server.py` + `run.sh`
6. `web/index.html`, `style.css`, `app.js`
7. `README.md`, `CHANGELOG.md`

---

## 17. End-to-End Verification

1. `python3 -m unittest discover tests -v` — every test passes. Paste the real output; do not
   claim success without it.

2. Build a fixture tree under the scratchpad directory
   (`/tmp/claude-1000/-home-gabriel--projects-propios-files-duplicated/…/scratchpad/e2e/`):

   | Path | Content | Expected classification |
   |---|---|---|
   | `A/docs/report.pdf` | `X` | `present_in_b` (B has it as `old_report.pdf`) |
   | `B/old_report.pdf` | `X` | — |
   | `A/photos/2024/beach.jpg` | `Y` | `exclusive` |
   | `A/copy1.bin` | `Z` | `exclusive` |
   | `A/nested/copy2.bin` | `Z` | `internal_copy` of `copy1.bin` |
   | `A/same-name.txt` | `P` | `exclusive` (B's `same-name.txt` holds `Q`) |
   | `B/same-name.txt` | `Q` | — |
   | `A/.git/config` | anything | offered in pre-scan |

3. `./run.sh`, open the printed URL, walk the whole flow: pick the three folders, send `.git`
   to trash, start the scan.

4. Confirm in the report: `report.pdf` is **not** listed as exclusive; `beach.jpg` and
   `same-name.txt` **are**; of `copy1.bin`/`copy2.bin` exactly one is ticked.

5. Apply, then verify on disk:
   - `DEST/photos/2024/beach.jpg` exists — relative structure preserved
   - **`B/` is byte-for-byte unchanged**
   - `DEST/_trash/.git/config` exists
   - `DEST/_report_*.json` matches what the UI displayed
