# The virtual bus: every bus semantic, no bus file

Decision (Tristan, 2026-10-20): the bus stops being a physical file. It keeps every semantic a bus has (one ordered sequence per root, append-only history, addressed messages, claims, keyed publication, pages, live delivery), and each of those semantics is declared once, as a capability class, over SQLite tables. This replaces the earlier plan to copy `bus.jsonl` into a table: that plan kept a file-shaped log with a JSON blob beside extracted columns, which is two copies of every addressed field. Audited against `origin/main` at `53f74dba4` (2026-10-20, 19:30 EDT).

## Why

The bus started on September 19 as an append-only JSONL file. Everything since has been the cost of the file being physical:

- **A proof layer for a second store.** The SQLite indexes over the file are a second store, so a layer proves after every crash or copy that the two still agree: prefix witnesses, seals keyed by inode, size and mtime, checked reads, tail replay. `private_bus_checkpoint.py` (1,316 lines), `checkpoint_seals.py` (218), `bus_page_index.py` (345), most of `wire_log.py` (1,009) and `wire_record.py`'s row reader. That layer froze the desktop for half an hour, and stranded the laptop's history on 2026-10-20 (#2333 is what fixed that restore).
- **Byte positions as identity.** `before_offset` (44 uses in 7 files), `max_bytes` (75 in 18), `through_bytes` (20 in 3), `byte_offset` (16 in 2), `after_offset` and `tail_bytes` (8 each). A message already has an identity, its `seq`.
- **Type identity read from key spellings.** `has_private_wire_fields(value)` (`bus_publication.py`, 7 uses) decides whether a row is a private delivery or a public message by probing a raw dict's keys (IMPL-1 at the boundary).
- **Facts kept by hand beside the data.** `bus_meta.json` (`WireMetadata`) holds `last_seq`, a hand-kept copy of `MAX(seq)`; three `Literal[1] | None` version markers (`writer_protocol_version`, `claim_envelopes_version`, `checkpoint_version`), which are format negotiation between our own components (TIME-4); and the checkpoint seal.
- **Two locks for one bus.** Writers lock `.bus.jsonl.lock` (`WireLog.locked()`). Readers, carries and seven classes lock `root / "wire"`, a path spelled by hand in each (`agent_activity.py:116`, `channel_management.py:134`, `comms.py:40`, `goal_management.py:72`, `history_views.py:201`, `coordination_tables/refusals.py:48`, `installer/upgrade.py:462`): MEMB-3.

In a database, a transaction already gives the bus atomic appends, a total order and crash consistency. So every item above is deleted, not ported.

## The bus semantics, one capability each

Each semantic is a class. A record kind is a family member that inherits the capabilities it has. Each capability owns its own table, keyed by `seq`, with only the columns it declares, so a query about one semantic reads one table, whatever the record's kind. The kind's declared name is derived (MEMB-4), and the MRO decides which capability tables a record spans. This is the multiple-inheritance plan's field-group mixins applied to the bus: `Addressed` here is the same class as that plan's `Addressed` (`sender`, `target`, 11 records), declared once for both.

| Capability | What it owns | Replaces |
|---|---|---|
| `BusRecord` (root) | `bus_rows(seq INTEGER PRIMARY KEY AUTOINCREMENT, kind, at)`; seq assigned by SQLite inside the append | `WireScan`'s hand monotonic check, `sequence_after`, `last_seq` |
| `AppendOnly` | `BEFORE UPDATE` and `BEFORE DELETE` triggers derived from the capability, raising | the append-only promise held by file mode |
| `Addressed` | `addressed(seq, sender, target)` plus the inbox index | sender/target read out of JSON at every reader |
| `KeyedPublication` | `UNIQUE(publication_key)`; republishing a key returns the committed row | `record_key`, `seen_keys`, `add_unique`, and the UNKNOWN reservation in `last_seq` (an uncertain commit becomes a lookup by key) |
| `Claimed` | claim transitions, written in the append's transaction | checkpoint claim rows, `claimed_after_seq`, `require_claim` |
| `Delivered` | a private delivery's `audience`, `decisions` and `control` | the private half of `CommittedDelivery`; `decisions_digest` (a hash of `decisions`), `resolver_version`, `policy_version`, `manifest_codec` (TIME-4) and `wire_root_id` are deleted, not stored |
| `PublicRecord`, `KeyedPublicRecord`, `PrivateRecord`, `KeyedPrivateRecord` | the four record kinds, each only its bases: `BusRecord` + `Message` (+ `Delivered`) (+ `KeyedPublication`) | `has_private_wire_fields`, `RedactedPublicWireRecord` decided by key probing |
| `Paged` | one keyset query, `WHERE seq < ? ORDER BY seq DESC LIMIT ?`, on `BusRecord` | `bus_page_index.py` and every byte-budgeted page |
| `Watched` | the store's committed-change signal (`TypedStore.transaction` already touches `.changed`) | `wire_watch.py` following file growth |
| `Activity` | its own capability table under the same root | `activity.jsonl` and `activity_checkpoint.py` |

**Message is the record, not a copy of it.** `Message` declares the fields a bus message has; its bases are the bus capabilities (`class Message(Addressed, BusCapability)`), so its own fields are its table and `Addressed` is a table every addressed record shares. No field list sits beside `Message`. `seq` belongs to `bus_rows` and is excluded from every capability table by derivation from `BusRow`'s fields. Structured fields (`mentions`, `task`, `report`, `answers`) are typed columns through `TypedTable`'s existing `JsonStorage` and `MessageKind` through `FamilyClassStorage`: one encoded field per column, never the whole record.

**No blob.** A row's fields are its capability tables' columns, and `FieldCodec` decodes a record from them. Storing the encoded record beside extracted columns would be a second writable copy of the same fields (IDEN-5). External transcripts (Claude, Codex, Pi sessions) are not ours and stay files.

**Durable and derived, by inheritance.** The bus tables are `DurableTables` (#2334). Every index over them (display counts, route counts, wake candidates) is a SQL index on a capability table, or a `DerivedTables` member rebuilt from the bus tables. Derived tables never need a carry.

**One write path, one lock.** A publish is one `BEGIN IMMEDIATE` transaction: the `bus_rows` row, its capability rows, claim transitions and same-transaction counters. `BEGIN IMMEDIATE` is the bus lock. `.bus.jsonl.lock`, `root / "wire"` and `_wire_lock_path` are deleted. Other stores keep their own files and declared lock order.

**Archives are a connection mode, not a parallel class.** An archived bus is the same database opened read-only. `ArchivedAccess` and `WritableAccess` collapse into how the connection is opened; the practice copy is a SQLite backup, like every other database `practice_copy` already copies.

**Plain names.** `WireLog`, `WireRecord`, `WireMetadata` and `wire_*` name the file mechanism. The owner becomes `Bus` (`bus.py`), records are `BusRecord` members, and no `wire` name survives inside the bus. External contracts keep their spelling.

## Deleted, not ported

- `private_bus_checkpoint.py`, `checkpoint_seals.py`, `bus_page_index.py`, `bus_meta.json` and `WireMetadata`, `WireRecord.rows`, `require_row_size`, `page_records`, `WireScan`, `has_private_wire_fields`, every `_wire_lock_path`.
- Tonight's checkpoint work (#2333's `CheckpointSchema`, #2334's checkpoint adoption) goes with the checkpoint. It bought the laptop restore, and the virtual bus makes it unnecessary.
- `tools/cutover/carry_context_store.py`, once the laptop has run it (it has, 2026-10-20 19:05) and every other bus has too.

## Banned shapes

- A JSON blob column beside typed columns.
- Two backends (`JsonlBus` and `SqliteBus`) or any flag choosing between them. One bus, no compatibility path.
- An adapter handing readers bytes or offsets "for now".
- A `kind` string compared by readers. They ask the member or dispatch with `@handles`.
- `Optional` seq or `None` for absent records. A missing record is a declared member or a raised lookup.
- A hand-written list of capabilities or tables. They are derived with `members_with`.

## Moving the existing history (the one data migration)

One carry step, run by the installer:

1. On the desktop, move `bus.jsonl` and `activity.jsonl` read-only to the HDD first. The SSD can't hold the file and the database at once.
2. Stream each row through `FieldCodec` into its declared record class and insert it with its original `seq`, in batched transactions.
3. While streaming, build the byte-offset-to-`seq` map and use it once to rewrite the stored rows that still hold offsets (wake candidates). Every derived index is rebuilt from the new tables, never converted.
4. Verify before the installer reports success: the row count equals the line count, `seq` is continuous, and every record re-encodes through `FieldCodec` to the bytes it was read from.
5. Order: the laptop first (13 messages since its context carry), then gaming, then the desktop. The old files stay on the HDD until both machines have run on the virtual bus for a day.

## Who does what

Pi GPT units (Tristan, 2026-10-20), ordered through `agent-comms-commander order send` with counted deadlines and exact target shapes, as the commander rules require. The interface comes first and everything else codes against it.

| Unit | Work |
|---|---|
| A | `Bus`, `BusRecord`, the capability classes and their derived tables, and the append, page, lookup-by-key and watch signatures. Published before anyone else starts. |
| W | The one writer onto `Bus.append`: `WireLog.append_private_unlocked` (`wire_log.py:768`), called from `bus_publication.py` and `publisher.py`, writes the four record kinds. Readers move after it, because a reader cut over before the writer reads a store nothing fills. |
| B1 | Delivery, wake, cursors and notification readers onto `seq` and the capabilities: `wake_candidate_index`, `notification_assignment`, `read_ledger`, `cursor_publication`, `goal_waits`, `store_order`. |
| B2 | History, presentation and transcript readers: `historical_views`, `presentation`, `transcripts`, `transcript_sources`, `active_route`, `acp`, `comms`, `message_bus`, `publisher`, plus `scripts/attach_historical_views.py`. |
| C | Deleting everything under "Deleted, not ported", with the derived indexes rebuilt as `DerivedTables`. |
| D | The carry, the HDD staging and the verification. |
| E | `Watched` replacing `wire_watch.py`'s file following. |

Everything lands as one batch through the merge queue, drop-on-fail. Then one install per machine in the order above.

## Done means

Each phase, its predecessors and the evidence on `origin/main` that proves it done are declared in `src/agent_comms/plan_phases.py`; the commander's plan-idle rule and order refusals read that family, and accepting a task never marks a phase done.

- `BusDeletionPhase.evidence` finds the five deleted modules absent and no known physical bus symbols, filename literals or lock-path expressions in Python source. Its AST check reads executable uses, including `/ "wire"`, `.with_name("wire")` and `.joinpath("wire")`, without matching prose or its own pattern declaration. This is a known-syntax screen; aliases and dynamically constructed paths still require source review. Remaining byte offsets and budgets are judged by their actual owner, since external native transcripts stay files.
- The deleted modules are gone.
- A process killed mid-publish leaves the whole message or none of it.
- Concurrent bus publication must not abort native cursor settlement. The current path is `CoordinatedTurn.cursor_status_for` → `CursorDelivery.advance` → `CursorOwner.advance_async` → `CursorOwner._advance_owned`. Bus reads use `Bus.observing` and SQLite snapshots; the cursor owner commits through the coordinator session's existing write transaction. `Coordination.run_async` runs this work through its worker with the default `StoreWait` contention policy. Qualify that path under competing writers within its bounded wait; no physical bus lock is reintroduced.
- Every history converts with every record round-tripping.
- On every machine: Toad pages history, delivery wakes units, claims hold, and a real message to a live agent is answered.
- Lines deleted are reported first; the census confirms.

Historical regression evidence: on core-5d8da75aa, the former
`NativeSourceCursor._advance_proven` in `native_source_cursor.py` took its
boundary with `blocking=False`, without contention allowance when no input
was committed. Concurrent writers ended turns with `BlockingIOError` three
times between 12:20 and 12:39 on 2026-10-21, with no install running. That
class and file have since been deleted; the incident motivates the current
contention obligation above, rather than describing a current source site.

## Deletion checkpoint from the current integration branch

At `acb2ddd0e`, all five old modules are already absent. Three physical bus locks
remain: `NativeReplyPreparation.finished`, `SendRefusal.record`, and
`CollaborationLedger.merge`. The last uses `.with_name("wire")`, which the old
grep misses. The old grep also matches its own `bus_meta` pattern declaration,
so its result does not establish whether physical bus uses remain.

The deletion branch removes those locks through the existing owners. Unread
publication keeps its local preparation lock, source revision check and SQLite
write transaction; a busy SQLite writer leaves completed readers pending.
Refusal recording keeps maintenance permission under the registry lock until
its SQL transaction finishes. Ledger merge keeps the registered-author snapshot
through the existing document update, in the declared registry-before-ledger
lock order. None creates a replacement bus lock.

The phase check reads committed source through `MainTree`. Its controls include
the actual remaining path spellings, physical filenames and removed symbols,
alongside comments and docstrings that describe deletion. This source checkpoint
does not mark the phase complete on main or qualify an installed runtime. The
merger still checks the affected copied native/Toad journey, including a fresh
DM, its actual reply and tab return.
