# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

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
