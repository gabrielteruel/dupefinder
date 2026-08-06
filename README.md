# dupefinder

**Find the files you have in one folder but not in another — compared by content, not by
filename — and move them somewhere else, with a full review step before anything is touched.**

You have two folders that overlap: an old backup and a current one, a phone dump and a photo
library, two external drives you've been copying between for years. You want to know what's in
the first that's genuinely missing from the second, and pull exactly those files out.

Sorting that out by filename doesn't work, because filenames lie in both directions:

| In folder A | In folder B | Filename says | Reality |
|---|---|---|---|
| `IMG_4021.jpg` | `beach-sunset.jpg` | different files | **same file, renamed** |
| `notes.txt` | `notes.txt` | same file | **different content entirely** |
| `report.pdf` | *(absent)* | — | genuinely missing from B |

dupefinder ignores names and identifies files by the **SHA-256 of their contents**, so all
three cases come out right. It runs as a small local web app: you pick the folders, review a
table of exactly what it found, tick what you want, and only then does anything move.

## Requirements

- **Python 3.10 or newer** — standard library only. No `pip install`, no `npm install`, no
  external dependencies of any kind. (Developed and tested on 3.12.)
- A web browser.

Runs natively on Linux, macOS and Windows, and on Windows via WSL.

## Quick start

```sh
git clone https://github.com/gabrielteruel/dupefinder.git
cd dupefinder
./run.sh
```

On Windows, use the bundled launcher instead:

```bat
run.cmd
```

It starts a server bound to `127.0.0.1` and prints the URL, opening your default browser
automatically on Linux, WSL, macOS and native Windows alike.

```sh
./run.sh --port 8080     # use a specific port
./run.sh --no-browser    # never try to open a browser
```

Nothing leaves your machine — the server only listens on loopback, and the page makes no
external requests.

## Walkthrough

1. **Choose a tool.** Compare two folders, or find duplicates inside a single folder — pick one
   before anything else. Each option explains what it does; you can switch back later without
   losing what you've typed.
2. **Choose folders.** Folder A (scanned for files to move), Folder B (compared against, never
   modified), and a Destination. Type a path or use the built-in folder browser. (Dedupe mode
   asks for Folder A and a quarantine folder only — there's no Folder B to compare against.)
3. **Review folders to skip.** dupefinder flags directories that are usually noise — `.git`,
   `node_modules`, caches, `$RECYCLE.BIN`, hidden folders — and lets you choose per directory:
   compare it, skip it, or (Folder A only) move it aside to a trash folder.
4. **Review and select.** A sortable, filterable table of every file in A that isn't in B (or,
   in dedupe mode, the duplicate groups to resolve — see below). Files that duplicate *other
   files within A* are shown too, but only one representative per duplicate group is
   pre-selected, so you don't move five copies of the same thing.
5. **Done.** A summary of what moved, plus a JSON audit report of the entire run.

A step indicator at the top of the page always shows where you are in this sequence.

## Dedupe mode

Sometimes there's no second folder to compare against — you just have one messy folder with
copies of the same file scattered across it: a photo library re-imported twice, a project
directory with old `backup/` subfolders nobody cleaned up. Pick "Find duplicates" on the first
screen to switch dupefinder to that mode: it scans a single folder and groups files by content,
wherever they landed inside it.

For each group of identical files, dupefinder needs to know which copy to *keep* — the rest go
to quarantine. Click "Keep everything in this folder" next to any copy to add its folder as a
priority rule (e.g. `fotos/originales`); for every group, the copy in the highest-priority
matching folder wins, and rules can be reordered or removed afterward. If no rule applies to a
group, the copy closest to the folder's root wins. Groups made entirely of zero-byte files are
shown but never auto-resolved, since every empty file has the same "content" and there's nothing
meaningful to prefer.

Nothing moves until you press "Move duplicates to quarantine" — the same read-first, review,
then-act flow as comparing two folders, the same collision handling, and the same JSON audit
report, which also records the keep-priority rules used so every decision can be reconstructed
later. dupefinder will not let a selection empty an entire group of files with real content;
at least one copy always survives (an all-zero-byte group, like a stray set of `.gitkeep`
files, is shown read-only and can't be selected at all).

## Safety

Moving files around is the kind of thing you only get to do wrong once, so the guarantees are
deliberately narrow:

- **Folder B is never modified.** Not moved from, not deleted from, not written to.
- **Nothing on disk changes until you press "Move selected files."** Scanning and reviewing the
  report are strictly read-only.
- **Nothing is ever permanently deleted.** Directories you send to "trash" are *moved* to
  `<destination>/_trash/`, never removed.
- **Name collisions never overwrite.** If a file already exists at the destination, dupefinder
  compares contents: identical means skip, different means it lands as `name_1.ext`.
- **Every run writes a JSON audit report** to `<destination>/_report_<timestamp>.json`, listing
  exactly what was moved, skipped, renamed or trashed.

## How it works

Hashing every file would be painfully slow, so comparison runs in four stages, each only
handling what the previous one couldn't already decide:

1. **Size buckets.** Two files of different sizes can never be equal. A file whose size is
   unique within A and absent from B is exclusive — decided with **zero bytes read**.
2. **Partial hash** (first 64 KiB) for the files that do share a size with something else.
3. **Sampled hash**, for files above 8 MiB that still share a size and a partial hash: two more
   64 KiB reads, from the middle and the end of the file, to rule out non-matches cheaply before
   committing to a full read.
4. **Full SHA-256**, only for files that also share a partial hash and, where applicable, a
   sampled hash.

On a typical folder of photos or documents, the vast majority of bytes on disk are never read
at all.

**Why large files are still read in full.** Two files that share a size and an identical first
64 KiB are usually — but not always — the same file. Disk images, preallocated downloads and
some video containers can match for a long way and diverge later, so dupefinder never declares
two files identical from a sample. Sampling is used only to prove files *different* cheaply:
for anything above 8 MiB it checks the middle and the end of the file first, which rules out
most non-matches in a fraction of a second instead of minutes. Anything that survives that is
read in full, because a wrong "you already have this" is the one error that could cost you a
file later.

## Resuming an interrupted scan

Every hash dupefinder computes is cached in a local SQLite database, kept under your user
cache directory — **never on the scanned drive itself**. Kill a scan partway through and
re-run it: already-hashed files are skipped, and the scan picks up close to where it left off.
Re-scan the same drive again later, weeks on, and only files that changed (by size and
modification time) get re-hashed — everything else is served straight from the cache.

The "Performance" panel shown before a scan lets you clear the cache or see how much it holds,
and a "Reuse cached hashes" checkbox is the escape hatch for the rare case where you want every
file re-read from scratch regardless of what's cached.

### Concurrent reads

The pre-scan "Performance" panel suggests an `io_workers` value based on the detected disk
type. The right number of concurrent readers depends on what's actually slow:

- On a network share, WSL-mounted Windows drive, or SSD/NVMe, the bottleneck is usually
  **latency** per request, not raw throughput — several reads in flight at once can finish
  faster than one at a time.
- On a spinning disk (HDD), concurrency usually **hurts**: what looks like several parallel
  reads becomes the drive head seeking back and forth between them, which is slower than reading
  one file through to the end before starting the next.

The default (`io_workers=1`) matches previous behavior exactly and is always safe; raising it is
an opt-in tradeoff, not a universal win.

The estimated time remaining shown during comparison is deliberately conservative: it's computed
from an upper bound on the bytes left to resolve, so it's expected to drift *downward* as a scan
progresses rather than staying fixed or climbing.

## Platform notes: external drives on WSL

WSL's automount only runs when WSL boots, so a drive plugged in afterwards won't appear under
`/mnt` on its own, even though Windows sees it fine.

For a drive Windows can read (NTFS, exFAT, FAT32) — if it's `D:` in Windows:

```sh
sudo mkdir -p /mnt/d
sudo mount -t drvfs D: /mnt/d
```

It then shows up as **Drive D:** in the folder picker. Unmount with `sudo umount /mnt/d`.

For a filesystem Windows can't read (ext4, say), use
`wsl --mount \\.\PHYSICALDRIVE2 --partition 1` from an administrator PowerShell instead.

### Keep the destination on the same drive

This matters more than it sounds. WSL reaches Windows drives over a 9p filesystem that is much
slower than native Linux storage. If Folder A is on a USB drive and the destination is on
`/home`, every moved file is copied byte for byte across that boundary — hours, for a large
library.

Put the destination on the **same drive** as Folder A and each move becomes a rename: instant,
whatever the file size.

```
Folder A:      /mnt/d/photos
Destination:   /mnt/d/_exclusive     ← same drive, instant moves
```

The destination can't be *inside* Folder A, but a sibling directory like the one above is fine.

## Running the tests

```sh
python3 -m unittest discover tests -v
```

## Project layout

```
dupefinder/    scanning, hashing, comparison, moving, and the HTTP server
web/           the static frontend (no build step)
tests/         unittest suite
docs/          design rationale
```

[`docs/design.md`](docs/design.md) covers why the tool is built this way — the comparison
pipeline, the safety guarantees and how they're enforced, the persistent cache and concurrency
model, and some non-obvious platform behaviour (WSL especially).

## License

[MIT](LICENSE) — free to use, modify and distribute, including commercially.

