# dupefinder

Find the files in **Folder A** that don't exist in **Folder B**, then move exactly those
into a third folder — reviewed and confirmed in a local web UI before anything is touched.

## Why content, not filenames

Comparing by filename doesn't work: the same file may have been renamed, and two files can
share a name while holding completely different content. dupefinder identifies files by
**SHA-256 of their content**, so a renamed file is correctly recognized as "already in B,"
and a same-named-but-different file is correctly recognized as exclusive to A.

## Requirements

- Python 3.12, standard library only. No `pip install`, no external dependencies.
- A web browser. No `tkinter` is required or used.

## Quick start

```sh
./run.sh
```

This starts a local server on `127.0.0.1` and opens your browser to it automatically. If it
can't open a browser, it prints the URL to open manually.

Options:

```sh
./run.sh --port 8080     # use a specific port
./run.sh --no-browser    # don't try to open a browser
```

## Walkthrough

1. **Choose folders.** Pick Folder A, Folder B, and a Destination folder, either by typing
   a path or using the built-in folder browser.
2. **Review folders to skip.** dupefinder flags directories that are typically noise —
   `.git`, `node_modules`, caches, hidden folders — and lets you choose, per directory,
   whether to compare it, skip it, or (for Folder A only) move it straight to a trash
   folder. Folder B can never be moved to trash; it is never modified.
3. **Review and select files to move.** A sortable, filterable table lists every file in A
   that isn't in B. Files that are exact content duplicates of other A files are shown too,
   but only one representative per duplicate group is pre-selected. Tick whichever files you
   want moved.
4. **Done.** A summary of what was moved, skipped, renamed, or trashed, plus the path to a
   JSON audit report of the whole run.

## External drives (USB) on WSL

WSL's automount only runs when WSL boots, so a drive plugged in afterwards will not appear
under `/mnt` on its own — even if Windows sees it perfectly well.

For a drive Windows can read (NTFS, exFAT, FAT32), mount it with `drvfs`. If the drive has
letter `D:` in Windows:

```sh
sudo mkdir -p /mnt/d
sudo mount -t drvfs D: /mnt/d
```

It will then show up as **Drive D:** in the folder picker. To unmount: `sudo umount /mnt/d`.

For a drive formatted with a filesystem Windows cannot read (ext4, for example), use
`wsl --mount \\.\PHYSICALDRIVE2 --partition 1` from an administrator PowerShell instead. That
detaches the disk from Windows and hands it to WSL directly.

### Put the destination on the same drive

This matters much more than it sounds. WSL reaches Windows drives over a 9p filesystem, which
is considerably slower than native Linux storage. If folder A is on the USB drive and the
destination is on `/home`, every single moved file is copied byte for byte across that boundary
— for a large photo library that can take hours.

If the destination is on the **same drive** as folder A, each move is just a rename: instant,
regardless of file size.

```
Folder A:      /mnt/d/photos
Destination:   /mnt/d/_exclusive     ← same drive, instant moves
```

The destination may not be *inside* folder A, but a sibling directory like the one above is
fine.

## How it works

Hashing every file is expensive, so comparisons run through three stages, each one only
processing what the previous stage couldn't already decide:

1. **Size buckets.** Two files of different sizes can never be equal. A file whose size is
   unique within A and absent from B is exclusive — decided with zero bytes read.
2. **Partial hash** (first 64 KiB) on the files that share a size with something else.
3. **Full SHA-256**, only on files that also share a partial hash.

On a typical folder of photos or documents this avoids reading the vast majority of the
bytes on disk.

## Safety

- **Folder B is never modified.** Not moved from, not deleted from, not written to.
- **Nothing on disk changes until you press "Move selected files."** Scanning and reviewing
  the report are entirely read-only.
- **Nothing is ever permanently deleted.** Directories you send to "trash" are moved to
  `<destination>/_trash/`, not removed.
- **Every run writes a JSON audit report** to `<destination>/_report_<timestamp>.json`
  listing exactly what was moved, skipped, renamed, or trashed.

## Running the tests

```sh
python3 -m unittest discover tests -v
```

## Project layout

```
dupefinder/    scanning, hashing, comparison, moving and the HTTP server
web/           the static frontend served by the app (no build step)
tests/         unittest suite
```
