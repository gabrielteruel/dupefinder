# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.4.0] - 2026-08-04

### Added

- Persistent SQLite hash cache with automatic resume: an interrupted scan
  picks up where it left off, and re-scanning an unchanged drive skips every
  file that hasn't changed. Cached hashes are keyed by path, size and
  modification time, and stored outside any scanned volume.
- Disk-type detection (`dupefinder/diskinfo.py`) with a suggested `io_workers`
  value surfaced in a new pre-scan "Performance" panel.
- Opt-in `io_workers` concurrency for the hashing stages, via a
  `ThreadPoolExecutor`. Default remains 1 (no behavior change).
- Sampled-region pre-filter for files above 8 MiB: files that share a size and
  a 64 KiB prefix are now discriminated by two extra seeks (middle and final
  64 KiB) before any full read. Purely eliminating — identity is still decided
  by the full SHA-256, never by a sample.
- Byte-based progress during the comparing phase, showing bytes processed,
  bytes remaining, total bytes to compare, throughput and an estimated time
  remaining, computed server-side with a rolling-window throughput estimate.
- macOS and native Windows support: browser opening, drive listing,
  `run.cmd`.
- `POST /api/volumes`, `GET`/`POST /api/settings`, `GET /api/cache/stats`,
  `POST /api/cache/clear` endpoints.

### Fixed

- `ValueError` crash comparing paths across Windows drives
  (`os.path.commonpath` on `C:\` vs `D:\`).
- Case-insensitive path containment checks on Windows.
- `HashCache` was not safe for concurrent access; the counters and digest
  dicts are now guarded by a lock that never blocks the actual file reads.
- The report table no longer freezes the browser on a large scan. Only the
  visible rows are rendered, so a report with 100,000+ files stays responsive.
- Native `alert()`/`confirm()` popups replaced with in-page dialogs that match
  the rest of the UI and support Escape and Enter.

## [0.3.0] - 2026-08-04

### Fixed

- Buttons that trigger a request are now disabled for the whole duration of that request,
  including the scan's progress polling. Clicking Continue repeatedly no longer fires several
  overlapping pre-scans.
- `POST /api/apply` can only run once per scan. A concurrent or repeated call is rejected with
  409 instead of attempting to move the same files twice.
- `POST /api/scan` returns the job already running when the request is identical, rather than
  spawning a second scanning thread over the same tree.
- `POST /api/prescan` refuses a duplicate request while an identical one is still walking.
- A directory that cannot be moved to `_trash/` is now recorded as an error instead of raising
  a 500, and no longer discards the directories that were trashed successfully.
- Folder-browse requests no longer stack up when clicking quickly through directories, which
  could render a folder other than the one last clicked.

## [0.2.0] - 2026-08-04

### Added

- Folder picker now lists every Windows drive actually mounted under `/mnt`, so external USB
  drives appear as shortcuts once mounted. Stale mount points and WSL's own `/mnt/wsl` and
  `/mnt/wslg` are excluded.
- README section on mounting external drives under WSL, including the guidance to keep the
  destination on the same drive as folder A so that moves are renames rather than copies.

### Changed

- The file counts shown for noisy directories during the pre-scan now stop at 2000 entries and
  are displayed with a trailing `+`. Previously an external drive's `$RECYCLE.BIN` could stall
  the pre-scan screen while it was walked in full over a slow filesystem.

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
