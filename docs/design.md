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

## 2. The four-stage pipeline

Hashing every file would mean reading every byte of every file, which on a large photo library
is hours of I/O. Instead, comparison discards candidates as cheaply as possible and only reads
bytes when it has to:

1. **Size buckets.** Two files of different sizes can never be equal. A file whose size is
   unique within folder A and absent from folder B is exclusive — decided with **zero bytes
   read**.
2. **Partial hash** (first 64 KiB) for files that share a size with something else.
3. **Sampled hash**, for files above `SAMPLE_THRESHOLD` (8 MiB) that still share a size and a
   partial hash: two more 64 KiB reads, from the middle and the final 64 KiB of the file.
4. **Full SHA-256**, only for files that also share a partial hash and, where the sampled stage
   ran, a sampled hash.

Stage 4 is not optional. Two different files can share their first 64 KiB — container formats
with large fixed headers do this routinely — so a partial-hash match proves nothing on its own.
There is a test pinning this exact case.

The sampled stage is not an identity decision either. It reads the middle and final 64 KiB
of files above 8 MiB, which is enough to prove two files *different* very cheaply, and never
enough to prove them the same. Anything that survives it is still read in full. Two files
that share a size, a 64 KiB prefix and both sampled regions are overwhelmingly likely to be
identical — but "overwhelmingly likely" is not the guarantee this tool makes, and a wrong
"you already have this" is the one error that can cost the user a file later.

On typical data the vast majority of bytes on disk are never read.

### Status vocabulary — exact strings the UI depends on

Nothing else in the repo defines these four strings, yet `comparer.py`, `server.py` and
`web/app.js` all depend on them matching exactly.

| `status` | Meaning | Ticked by default |
|---|---|---|
| `exclusive` | Content not present in B; first representative of its hash within A | **yes** |
| `internal_copy` | Content not present in B, but another A file already represents this hash | no |
| `present_in_b` | Content exists in B | no (hidden from the main table) |
| `unreadable` | The A file could not be read; classification impossible | no |

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
code rather than by convention. These are the seven hard rules the project binds itself to.
Violating any of them is a bug, not a trade-off:

| # | Guarantee | Where it lives |
|---|---|---|
| 1 | Folder B is never modified — not moved from, not deleted from, not written to, ever | `move_to_trash()` raises on any directory whose root is `"B"` |
| 2 | Nothing on disk changes until `POST /api/apply` is called | Scanning and reporting perform no writes at all |
| 3 | Nothing is ever permanently deleted | "Delete" moves directories into `<destination>/_trash/` instead |
| 4 | All code, comments, docstrings, UI strings, README and CHANGELOG are in English | Convention, checked in review |
| 5 | Zero third-party dependencies — standard library only | No `requirements.txt`, no `pyproject.toml` dependency list, no CDN links; see §9 |
| 6 | Symbolic links are never followed (files or directories) | `scanner.scan()` skips symlinks explicitly — prevents infinite loops and duplicate accounting |
| 7 | A read error never aborts a scan | Recorded as `unreadable`, walk continues |
| — | Collisions never overwrite | Contents compared: identical → skip, different → `name_1.ext` |
| — | Every run is auditable | JSON report written to `<destination>/_report_<timestamp>.json` |

Four consequences worth stating explicitly:

- **Destination containment is validated up front.** The destination may not be inside folder A
  or folder B, and A and B may not contain each other — otherwise the tool would be moving
  files into the tree it is still reading.
- **A read error never aborts a scan.** It is recorded and the walk continues. A single
  unreadable file in a 200,000-file tree must not cost the user the entire run.
- **The persistent cache is read-only with respect to the scanned volumes.** It stores digests
  in a database under the user's cache directory and never writes to, renames within, or deletes
  from folder A or folder B. Rule 2 still holds with the cache enabled.
- **A misclassification caused by a stale cache entry or a hash collision can never destroy
  data.** Folder B is untouched, nothing is deleted, and a name collision at the destination is
  resolved by suffixing, never by overwriting. The "Reuse cached hashes" checkbox is the escape
  hatch when certainty matters more than speed.

## 5. Decisions already made — do not revisit

These were settled during the original build and re-confirmed for the resumability work. Each
has a one-line reason; the reason is the part that stops someone "fixing" it later.

- **Unreadable B file.** It is dropped, so an A file that is actually its duplicate is classified
  `exclusive` and may get moved. This is the safe direction: B is untouched and the A file merely
  relocates to the destination, fully recoverable.
- **Empty files (size 0).** They share one digest and fall out of the generic bucket logic
  correctly. No special case; there is a test for this.
- **Representative selection** for `internal_copy` is the lexicographically smallest `rel_path`,
  which is why the scanner sorts. It must be deterministic across runs.
- **Files at or below `PARTIAL_SIZE`** have identical partial and full digests by construction.
  Harmless: partial digests are only ever compared against other partial digests, and full
  against full. No special-case logic needed.
- **Apply ordering** inside `POST /api/apply` is **validate → create dest → move files → move
  trash → write report**. Trash is last so a validation or move failure leaves the noisy
  directories where they are.
- **`shutil.move`, never `os.rename`.** The destination is routinely on a different filesystem
  than the source (`/home` vs `/mnt/c`, or across Windows drive letters), where `os.rename`
  fails.

## 6. Concurrency

### Request concurrency

The server assumes overlapping requests will happen — a double-click, a stale browser tab, a
retry — and does not rely on the UI's button-disabling to prevent them.

- **`/api/apply` runs at most once per scan.** `applying` and `applied` are claimed atomically
  under a lock *before any file is touched*. Moving is destructive and not idempotent: a second
  pass would find its sources already gone. A duplicate call is refused with `409`.
- **`/api/scan` is idempotent.** An identical request while a scan is running returns the
  existing job rather than starting a second thread over the same tree.
- **`/api/prescan` is single-flight**, keyed by the folder pair, because walking both trees is
  expensive.

### Read concurrency

`io_workers` parallelises the hashing stages only — bucketing, representative selection and
classification stay sequential, so the output is identical for any worker count. There is a
test (`test_io_workers_four_matches_io_workers_one_on_every_scenario` in
`tests/test_comparer.py`) that pins this equivalence directly, comparing `io_workers=1` against
`io_workers=4` on the same input.

`HashCache`'s lock guards only the counters and digest dicts, never the `open()`/`read()` calls
that do the actual I/O, so concurrent reads genuinely overlap instead of being serialized behind
the cache.

The suggested-worker table in `diskinfo.py` follows one piece of reasoning: concurrency helps
when the bottleneck is *latency* — 9p round trips, network shares, NVMe queues that can serve
several requests at once — and hurts on a spinning disk, where "concurrent" reads become the
drive head seeking back and forth between them instead of reading sequentially. That table is
derived from reasoning about how each transport behaves, **not from a timing measurement on this
hardware.** A timing run that contradicts it — for example, timing the same scan at `io_workers`
1, 2, 4 and 8 on a real spinning USB disk and finding a higher worker count wins — should revise
the table with the measurement recorded. Otherwise it hardens into folklore.

## 7. Platform notes: WSL

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

`dupefinder/diskinfo.py` now implements exactly what this prescribes: it detects the WSL case
and queries Windows via PowerShell instead of trusting `rotational`, falling back to
`kind="unknown"`, `suggested_workers=1` on any failure — detection never guesses upward.

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

### The hash cache lives off `/mnt`

SQLite depends on POSIX advisory locks, which are not reliable over 9p/drvfs or network mounts.
The persistent hash cache lives under the user's platform cache directory, never on the volume
being scanned:

| Platform | Cache location |
|---|---|
| Linux / WSL | `$XDG_CACHE_HOME/dupefinder` if set, else `~/.cache/dupefinder` |
| macOS | `~/Library/Caches/dupefinder` |
| Windows (native) | `%LOCALAPPDATA%\dupefinder`, falling back to the user's home directory |

`--cache-dir` overrides this (mainly for tests). Every row is keyed by `(path, size, mtime_ns)`,
not by `(path, size)` alone: size alone can't detect a file that was edited back to the same
byte count, but a change in modification time almost always accompanies a content change and is
cheap to check on every scan (a single `stat()`, no read). A cache keyed only on `(path, size)`
would silently serve a stale digest for an edited-and-restored-size file — exactly the kind of
false "already have this" this tool is built to avoid.

## 8. Environment

`tkinter` is not installed on the development or target machine. No UI work — present or
future — may reach for it; the frontend is, and stays, the vanilla HTML/CSS/JS the browser talks
to over the HTTP API.

## 9. Why there are no dependencies

The "zero third-party dependencies" rule (§4, rule 5) was re-examined against this feature set
rather than applied reflexively — the verdict was no dependency earns its place, but the
reasoning differs per candidate:

| Candidate | What it would buy | Why not |
|---|---|---|
| Bootstrap 5 | Prebuilt components, grid | ~230 KB vendored, would fight the existing 6.8 KB dark theme, and solves none of this UI's actual problems (the report table freeze is a rendering-volume bug, not a styling one). |
| Pico.css / Water.css | Classless restyle | Would replace a working theme rather than fix anything — churn, not improvement. |
| A virtual-table library (Clusterize.js et al.) | Windowed rendering for the report table | The problem is real, but the fix is ~70 lines of vanilla windowing against the existing markup — less code than vendoring and integrating a library. |
| `platformdirs` | Cross-platform cache directory | Solves exactly the `cache_dir()` problem in 15 lines of stdlib that are already written and tested (§7). A dependency to save 15 lines fails the bar. |
| `xxhash` / `blake3` | 5–20x faster hashing | The bottleneck is the disk, not the CPU, on the hardware this targets — a USB HDD at ~100 MB/s against `hashlib.sha256` sustaining several hundred MB/s. Would buy nothing while invalidating every cached digest and the `sha256` field in every report and audit JSON. |
| `psutil` | Mount/partition enumeration | Does not expose the HDD/SSD rotational property detection actually needs, so the PowerShell / `diskutil` / `sysfs` paths would have to be written anyway. |
| FastAPI / Flask | Routing, validation, better DX | **The one place the rule has a real cost.** Both are genuinely nicer to write against than `BaseHTTPRequestHandler`. But the server already exists and works; rewriting it is pure regression risk for zero user-visible gain. Worth revisiting only if the API grows well beyond its current ~10 endpoints. |

Two structural blockers apply on the frontend side regardless of any single library's merits:
the README's promise that "the page makes no external requests" rules out anything served from
a CDN, and the absence of a build step or `npm` rules out anything needing compilation (Tailwind,
JSX, any bundler-dependent package). The point of recording this is that the next person to ask
does not have to repeat the analysis.
