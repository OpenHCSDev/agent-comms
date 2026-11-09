# Registry rows: one change writes one row (P1441)

Tristan, 2026-11-09: "anything that's quadratic needs to be obliterated."

## The defect, measured

The registry is one JSON document, `registry.json` (`RegistryFile`, `storage/locked_store.py:759`).
On the live bus it is 5.6 MB and holds 2,933 threads. Every change (a declare, a status change, a
claim) rewrites all of it (`RegistryStore.save_unlocked`, `registry/registry_store.py:590`):

| step | cost on the live bus |
|---|---|
| `FieldCodec.encode(document)`: walks every cached row | about 20 ms (7 us per row: 0.35 / 1.43 / 2.86 ms at 50 / 200 / 400 threads) |
| `json.dumps(raw, indent=2)` | 19 ms |
| private guard writes the file and digests all of it | 5.6 MB written and synced |
| `DocumentBytes.capture`: rereads the file to pin the revision | 5.6 MB read |

One row changes; 2,933 are paid for, so a bus's life costs O(n^2). #4479 removed the re-encode of
unchanged rows; the whole-document rewrite under it remains.

## Why this is a redesign, not a storage swap

Readers do not read the file; they read revisions of it. `RegistryCut`, `RegistryEntriesCut` and
`RegistryPairCut` pin `FileObservation`s, and `snapshot_pair` pins the registry and another
`LockedStore` at one pair of file revisions. "Did it change since I read" is "did the bytes change".
26 modules use the store or the document; 35 sites read `threads`/`statuses` directly.

## Target

- Thread rows, statuses, aliases, owners and claims are `TypedTable` rows in one SQLite file, keyed
  by thread name. A change is one transaction touching its rows.
- A revision is the table's commit counter, not a file stat. A cut is a read transaction pinned at
  that counter; a pair cut opens both stores' transactions in one declared order.
- The private guard (`registry/private_registry_guard.py`) has two roles. Its two-slot digest of
  registry.json's bytes is a crash-consistency protocol for atomically replacing a whole file
  (pending digest, rename, commit). Its slots hold (root_id, seq, phase, digest) and
  `registry_digest(path)` rehashes all 5.6 MB per uncached check. The guard's format and two-phase
  protocol stay; only what it digests changes: the row store's committed revision instead of the
  file's bytes. A write is guard `prepare(digest(r + 1))`, the row transaction, guard `commit`; a
  crash between them is today's abandoned-rename case and recovers the same way, and a write
  outside the store shows a revision the guard never committed, so it is refused. No guard carry.
  Its native-namespace and coordinator-cohort ownership stays.
- `RegistryDocument` stops being the stored form. Readers that need many rows ask the table
  (children of a parent, threads in a project) instead of walking a decoded document.

## Steps, each a commit on one PR

1. Table declarations for the five row families beside their current types; nothing reads them yet.
2. The revision and cut owners on the table. Every cut consumer goes through
   `RegistrySelection.matches_current()`/`current()`, which compare `revision` under the registry
   lock, so the lock protocol (`RegistryLocking`, pair cuts, `_capturing_sources`) stays and only
   the revision's value changes: the header row's counter, bumped in each write transaction,
   paired with the guard file as today. Each row carries `changed_at` (the revision that last wrote
   it); a reader holding revision r refreshes with `WHERE changed_at > r`, so a read costs the
   changes since it last read, not the whole registry (today's capture rereads all 5.6 MB).
3. (Mostly unnecessary, traced.) The 35 direct readers read the in-memory `RegistryDocument`
   (`document.threads`, `document.statuses`), not the file, and the document stays the in-memory
   model; only its storage and refresh change. Readers move only where one walks every thread to
   find a few (children of a parent, threads in a project) and a table query answers it.
   The guard's check is on the switch's path: `private_guard_unlocked` keys on
   `_link_revision(self.path)` (`registry_store.py:633`) and `PrivateRegistryGuard.load` verifies
   registry.json's digest against its slots, so the digest role leaves the guard's load and write
   in the same commit as the switch.
4. `save_unlocked` writes changed rows only; delete `ThreadRows`' raw/decoded cache and the JSON path.
   The seam (traced): each process caches its last `RegistryRevision` (`RegistryCache`); today a
   write anywhere makes every reader re-parse the whole file (`_cache_revision`:
   `FieldCodec.parse(source.content)`, ~31 ms at 5.6 MB) before reusing decoded threads, so a change
   costs O(registry) in every reader process too. With rows, `RegistryRevision` holds the row
   revision; a stale entry applies `RegistryRowStore.changes_since(stale.revision)` to a copy of its
   document. `save_unlocked` commits `ThreadRows.differing(original)` plus the changed statuses,
   aliases and generations. Precedent: presence already left registry.json for `PresenceStore`
   because every heartbeat rewrote the whole file.
   Pair cuts need no change: `CopyingStore._capturing_sources` (`storage/locked_store.py:1151`)
   takes the ordered locks and calls each store's `_capture_unlocked()`, and each store decodes its
   own capture through `read_copied`. `DocumentBytes` becomes one member of a capture family; the
   registry's member carries the row revision and `changes_since(cached revision)` instead of the
   file's bytes, so pair cuts, lock ranks and every other store keep their code.
5. Carry: `tools/cutover/registry_rows.py` reads `registry.json` once into the table and is deleted
   after the install that runs it. Rehearsed on a copy of the live registry before any install.

## Done

- Declaring a thread at 3,000 threads writes one row and costs the same as at 30 (measured).
- `grep -c 'filename = "registry.json"' src/agent_comms/storage/locked_store.py` is 0.
- The installed build declares a thread on the live bus and a real message is answered.
