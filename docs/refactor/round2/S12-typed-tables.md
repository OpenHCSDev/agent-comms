# S12: Typed tables

**Head audited:** `agent-comms` `main` at `a10655d`; re-verify at yours. **Rules:** [00-RULES.md](00-RULES.md). **Builds** [A13 `TypedTable`](02-SHARED-ABSTRACTIONS.md#a13-typedtable); **uses** A2.
**Step 1:** build A13 as a new module. **Step 2:** migrate every table no other active surface owns.

---

## What is wrong

Every SQLite table's shape is written out several times: in its DDL, in its class where one exists, in each positional `INSERT`'s value order, and in every reader's hand mapping from columns to fields.

- **57 tables; 321 reads by column name or index across 32 modules**, led by `coordination_store.py` and `optional_awareness_projection.py` (44 each), `historical_native_inputs.py` (30), `compaction_journal.py` (20), `ordinary_delivery_bridge.py` (19), `native_source_cursor.py` (18), `todos.py` (17), `coordinated_runtime.py` (14).
- **34 of 51 `INSERT`s are positional**, across 13 modules. Adding or reordering a column breaks them, or silently writes values into the wrong columns when the swapped columns share a type.
- **Classes that exist are rebuilt by hand at every reader:** `read_expected_prompt_binding` reads 13 columns into a `PromptBinding`, `_cursor_from_row` maps 12 into `CurrentNativeCursor`, `read_ordinary_delivery_candidate` maps all 7 of `OrdinaryDeliveryCandidate`'s stored fields one by one.
- **`native_runtime_inputs` has no row type at all**: 16 columns, one writer, eight modules reading it raw.
- **Join results have no types:** `optional_awareness_projection.py` reads projection rows whose shapes exist nowhere as classes.
- **Three columns keep a retired name:** `admission_epoch`, `owner_admission_epoch`, `sent_owner_admission_epoch`.

---

## Target

- **One row type per table**, a frozen dataclass on A2, and **A13 derives everything else from it**: the DDL (columns, keys, uniqueness, references, indexes and `STRICT`, declared as field or class metadata), strict reads, and inserts and updates with derived column lists.
- **Hand-written DDL is deleted.** So are the hand mappers (`_cursor_from_row` and every function like it) and every positional `INSERT`.
- **A row type for each projection query**, declared beside the query.
- **`NativeRuntimeInput`** declared as `native_runtime_inputs`' row type.
- **The retired names disappear by derivation:** the row types use `admission_generation` and its siblings, and the derived DDL follows.

---

## Cutover

Deriving the DDL changes schemas, so every table S12 touches is classified (rule 2):

- **Runtime and derived tables** (prompt bindings, native source cursors, ordinary delivery candidates, native runtime inputs, awareness projections, and the rest of the private N/K runtime state) **are reset** at cutover.
- **Durable tables** (for instance `todos.py`'s, if they hold the owner's tasks) keep their data through a one-shot tool in `tools/cutover/`, run once and then deleted.

No code in `src/` ever reads an old schema.

---

## Scope

**Every table in every file not owned by another active round-2 surface.** S9 migrates the compaction tables, S10 the tool broker's read, L0 anything inside its targets. Everything else is S12's, including `coordination_store.py`, `optional_awareness_projection.py`, `historical_native_inputs.py`, `ordinary_delivery_bridge.py`, `native_source_cursor.py`, `native_prompt_binding.py`, `todos.py` and `coordinated_runtime.py`.

Along the way, in S12's files: `ordinary_delivery_bridge.py::_committed_bus_row` reads a bus row raw (`from`, `id`, `to`); decode it into the bus's message record through A2.

---

## Guards

In S12's files as soon as it merges, and codebase-wide once S9 and S10 have adopted A13:

- no reads of a row by column name or index;
- no `INSERT` or `UPDATE` without a derived column list, and no positional `VALUES`;
- no `CREATE TABLE` outside A13.

---

## Tests

- **One family-level test for A13:** for every registered table, the derived DDL creates a table whose columns match its row type, and a row round-trips. That single test covers all tables; there is no per-table test.
- **One new-case test (T2):** a test-only row type gets DDL, reads and writes with no other edit.
- **Delete** the per-table tests of hand mappers, positional inserts and old schemas. Do not port them.
- Behaviour tests of what the stores are used for keep passing.

---

## Done when

A13 is merged; every table in S12's scope has a row type, derived DDL and typed access; no hand mapper, positional insert or hand-written DDL remains in its files; runtime stores have been reset and durable ones carried across by tools that are now deleted; the guards pass.

## Dispatch

> **`refactor-s12`:** Complete S12 per `docs/refactor/round2/S12-typed-tables.md`. Read `00-RULES.md` first. Land A13 first as a new module; S9 and S10 are waiting on it. Then migrate every table in your scope completely, deleting hand-written DDL, hand mappers and positional inserts as you go. Classify each store before its schema changes: reset runtime state, carry durable state across once with a tool you then delete.
