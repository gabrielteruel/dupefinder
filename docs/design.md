# Design notes

Why dupefinder is built the way it is. This documents decisions that were expensive to arrive
at and are easy to undo by accident — the reasoning, not the API surface. For what the tool
does and how to run it, see the [README](../README.md).

---

## 1. Identity is content, never names

A file's identity is the SHA-256 of its bytes. Filename, path, timestamp and size are never
used to decide whether two files are "the same" — only to decide whether the answer can be
reached cheaply.

This falls out of the actual problem: folders that have been copied, renamed and reorganised
over years. A renamed file is the same file. Two files called `notes.txt` usually aren't.

## 2. The three-stage pipeline

Hashing every file would mean reading every byte of every file, which on a large photo library
is hours of I/O. Instead, comparison discards candidates as cheaply as possible and only reads
bytes when it has to:

1. **Size buckets.** Two files of different sizes can never be equal. A file whose size is
   unique within folder A and absent from folder B is exclusive — decided with **zero bytes
   read**.
2. **Partial hash** (first 64 KiB) for files that share a size with something else.
3. **Full SHA-256**, only for files that also share a partial hash.

Stage 3 is not optional. Two different files can share their first 64 KiB — container formats
with large fixed headers do this routinely — so a partial-hash match proves nothing on its own.
There is a test pinning this exact case.

On typical data the vast majority of bytes on disk are never read.

## 3. Determinism

When several files inside folder A share identical content and none exist in B, exactly one is
offered for moving and the rest are marked as internal copies. The one chosen is the
**lexicographically smallest relative path**.

This is why `scanner.scan()` sorts its results before returning them. It is not cosmetic: an
unsorted walk would make the representative depend on filesystem iteration order, so the same
inputs could produce different output between runs. Anything that reorders the scan silently
breaks reproducibility.

## 4. Safety model

Moving files is hard to undo, so the guarantees are deliberately narrow and each is enforced in
code rather than by convention:

| Guarantee | Where it lives |
|---|---|
| Folder B is never modified | `move_to_trash()` raises on any directory whose root is `"B"` |
| Nothing changes until the user applies | Scanning and reporting perform no writes at all |
| Nothing is permanently deleted | "Delete" moves directories into `<destination>/_trash/` |
| Collisions never overwrite | Contents compared: identical → skip, different → `name_1.ext` |
| Every run is auditable | JSON report written to `<destination>/_report_<timestamp>.json` |

Two consequences worth stating explicitly:

- **Destination containment is validated up front.** The destination may not be inside folder A
  or folder B, and A and B may not contain each other — otherwise the tool would be moving
  files into the tree it is still reading.
- **A read error never aborts a scan.** It is recorded and the walk continues. A single
  unreadable file in a 200,000-file tree must not cost the user the entire run.

## 5. Concurrency

The server assumes overlapping requests will happen — a double-click, a stale browser tab, a
retry — and does not rely on the UI's button-disabling to prevent them.

- **`/api/apply` runs at most once per scan.** `applying` and `applied` are claimed atomically
  under a lock *before any file is touched*. Moving is destructive and not idempotent: a second
  pass would find its sources already gone. A duplicate call is refused with `409`.
- **`/api/scan` is idempotent.** An identical request while a scan is running returns the
  existing job rather than starting a second thread over the same tree.
- **`/api/prescan` is single-flight**, keyed by the folder pair, because walking both trees is
  expensive.

## 6. Platform notes: WSL

Two findings that are non-obvious and cost real debugging time.

### `/sys/block/*/queue/rotational` is unreliable inside WSL

The standard Linux way to tell an SSD from a spinning disk is that flag. **Inside WSL it
reports `1` (spinning) for virtual disks that are actually backed by an NVMe SSD.** Any logic
that trusts it under WSL will reach exactly the wrong conclusion.

Detecting the real media type from WSL requires asking Windows:

```powershell
$n = (Get-Volume -DriveLetter D | Get-Partition | Get-Disk).Number
Get-PhysicalDisk | Where-Object { $_.DeviceId -eq $n } | Select-Object MediaType, BusType
```

The `Where-Object` filter matters. `Get-PhysicalDisk -DeviceId` is not a valid parameter on all
PowerShell versions, and the `Get-Disk | Get-PhysicalDisk` pipeline association returns nothing
for USB-attached disks. The call costs roughly 1.7 s, so it belongs off the hot path.

### Windows drives are reached over 9p, and that changes what "slow" means

Under WSL, every `open()`, `stat()` and `read()` against `/mnt/<letter>` is a round trip to a
Windows-side process. The cost is **latency**, not bandwidth.

This has two practical consequences:

- Reading many small files is disproportionately expensive, since each one pays the round trip.
  This is why the pre-scan caps its informational file counts instead of walking huge
  directories in full.
- Moving a file between `/mnt/...` and the Linux filesystem copies every byte across that
  boundary, while moving within the same drive is a rename. Keeping the destination on the same
  drive as folder A turns hours into an instant operation.

### If persistence is ever added, keep the database off `/mnt`

SQLite depends on POSIX advisory locks, which are not reliable over 9p/drvfs or network mounts.
Any future on-disk cache must live under the user's home or cache directory, never on the
volume being scanned.
