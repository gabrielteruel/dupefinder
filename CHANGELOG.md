# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

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
