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

Runs on Linux, macOS and Windows via WSL.

## Quick start

```sh
git clone https://github.com/gabrielteruel/dupefinder.git
cd dupefinder
./run.sh
```

It starts a server bound to `127.0.0.1` and prints the URL. On Linux and WSL it also opens
your browser automatically; elsewhere, open the printed URL yourself.

```sh
./run.sh --port 8080     # use a specific port
./run.sh --no-browser    # never try to open a browser
```

Nothing leaves your machine — the server only listens on loopback, and the page makes no
external requests.

## Walkthrough

1. **Choose folders.** Folder A (scanned for files to move), Folder B (compared against, never
   modified), and a Destination. Type a path or use the built-in folder browser.
2. **Review folders to skip.** dupefinder flags directories that are usually noise — `.git`,
   `node_modules`, caches, `$RECYCLE.BIN`, hidden folders — and lets you choose per directory:
   compare it, skip it, or (Folder A only) move it aside to a trash folder.
3. **Review and select.** A sortable, filterable table of every file in A that isn't in B.
   Files that duplicate *other files within A* are shown too, but only one representative per
   duplicate group is pre-selected, so you don't move five copies of the same thing.
4. **Done.** A summary of what moved, plus a JSON audit report of the entire run.

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

Hashing every file would be painfully slow, so comparison runs in three stages, each only
handling what the previous one couldn't already decide:

1. **Size buckets.** Two files of different sizes can never be equal. A file whose size is
   unique within A and absent from B is exclusive — decided with **zero bytes read**.
2. **Partial hash** (first 64 KiB) for the files that do share a size with something else.
3. **Full SHA-256**, only for files that also share a partial hash.

On a typical folder of photos or documents, the vast majority of bytes on disk are never read
at all.

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
```

## License

[MIT](LICENSE) — free to use, modify and distribute, including commercially.

