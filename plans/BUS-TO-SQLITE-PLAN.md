# Moving the bus from JSONL files into SQLite

Decision (Tristan, 2026-10-20): the bus history moves from `bus.jsonl` and `activity.jsonl` into SQLite tables, so a message and every index that depends on it commit in one transaction. Audited against `origin/main` at 18:30 EDT.

## Why

The bus started on September 19 as an append-only JSONL file; SQLite arrived five days later for claims, goals and read state; indexes over the file followed as history grew. Because the file and its SQLite indexes are separate stores, a whole layer exists to prove they still agree after a crash or a copy: prefix seals keyed by device, inode and size, checked reads under one lock, tail replay in chunks. That layer is about 3,400 lines and is the failure class that froze the desktop for half an hour. With the history as rows, SQLite's own transaction does that job, and the layer is deleted.

## What stays exactly as it is

- **`WireLog` remains the one owner of bus persistence** (`wire_log.py`). Its storage changes; its role does not. The 30 modules that use it keep using it.
- **Messages keep their sequence numbers.** `seq` is already the identity 42 record types carry; it becomes the table's primary key.
- **The record shape and its codec.** Each message is encoded once by `FieldCodec`, as today.
- **External JSONL is not ours and stays a file:** Claude, Codex and Pi session transcripts are read where they are.

## The design

**One table, written in the same transaction as its indexes.** `BusRecordRow(TypedTable)`: `seq` primary key, the columns readers filter on (`root_id`, `sender`, `target`, kind, time), and the encoded record. Activity gets its own table on the same pattern. Both live in the same database as the claim and checkpoint tables they feed, so an append and its index updates commit together or not at all. WAL mode; appends take the existing declared store lock order (`locked_store`), so nothing new decides ordering.

**Byte offsets disappear from the API.** Every `after_offset`, `before_offset`, `position`, `through_bytes` and `fingerprint(offset)` becomes a sequence number or is deleted. `tail_bytes` and `newest_messages(max_bytes=…)` become "the newest N rows". This is the one change every reader feels, and it is mechanical once `WireLog`'s new signatures exist.

**Derived indexes become queries or same-transaction tables.**
- `bus_page_index.py` (byte offsets for history pages): deleted; a page is `ORDER BY seq LIMIT`.
- `private_bus_checkpoint.py`, `checkpoint_seals.py` (prefix witnesses, checked reads, tail recovery): deleted, except the claim lookup, which becomes a table updated with the append.
- `bus_display_index.py`, `activity_checkpoint.py`, `bus_projection.py`: queries over the table, or small tables updated in the append's transaction.
- `wake_candidate_index.py`: `byte_offset` and `through_bytes` become `seq`.

**Live delivery watches the database.** `wire_watch.py` already watches committed store changes since P336; it follows the bus table's commits instead of the file.

## Moving the existing history (the one data migration)

One carry step, run by the installer like any other:

1. Move `bus.jsonl` and `activity.jsonl` to the HDD first, read-only. The desktop's SSD cannot hold a 25 GB file and a 25 GB database at once; reading from the HDD and writing the database to the SSD needs no doubled space.
2. Stream the file into the table in batched transactions, each record decoded by `FieldCodec` and inserted with its own `seq`.
3. Build the byte-offset-to-`seq` map while streaming, and use it once to rewrite the few stored rows that still hold offsets (wake candidates, checkpoint claim rows). Every derived index is rebuilt from the table rather than converted.
4. Verify before the installer reports success: row count equals line count, `seq` is continuous, and every record re-encodes to the bytes it was read from.
5. The old files stay on the HDD until both machines have run on the database for a day, then they are deleted.

## Who does what, in one batch

Interface first, then everything in parallel against it.

| Unit | Work | Ceiling |
|---|---|---|
| A | `BusRecordRow`, the activity table, and `WireLog`'s sequence-based signatures, published first so others code against them | interface in 30 minutes, done in 2 hours |
| B1 | readers in delivery, wake, cursors and notification (`wake_candidate_index`, `notification_assignment`, `read_ledger`, `cursor_publication`, `goal_waits`, `store_order`) | 2 hours |
| B2 | readers in history, presentation and transcripts (`historical_views`, `presentation`, `transcripts`, `transcript_sources`, `active_route`, `acp`, `comms`, `message_bus`, `publisher`), plus `scripts/attach_historical_views.py` | 2 hours |
| C | deleting the consistency layer and replacing the derived indexes with queries | 1.5 hours |
| D | the conversion carry, the HDD staging and the verification | 2 hours, plus the conversion's run time |
| E | `wire_watch.py` on the bus table's commits | 1 hour |

All six land as one batch through the merge lane, drop-on-fail. Then one install on gaming, which runs the conversion on its smaller bus first; then the desktop.

## Done means

No byte offset into the bus in any signature or stored row. The consistency layer is gone. A process killed mid-append leaves the whole message or none of it. Both buses' histories converted with every record round-tripping. Toad pages history, delivery wakes units, and claims hold, on both machines. Lines deleted reported first; the census confirms.

## Ordering against the switch

The switch to the native commander and this migration touch different layers (commander tools against storage). The migration starts now and does not wait. If the parity gate passes before this batch lands, the switch goes first; otherwise the migration batch lands first and the gate runs on top of it.
