# Round 2 index

**Head:** `agent-comms` `main` at `15a4d00` (#225). **Binding rules:** [00-RULES.md](00-RULES.md). **Abstractions:** [02-SHARED-ABSTRACTIONS.md](02-SHARED-ABSTRACTIONS.md). **How it runs:** [03-COORDINATION.md](03-COORDINATION.md).

Every surface file names the head it was audited at. `main` moves fast; re-verify your file against `main` before editing and correct it in your PR where it is wrong.

---

## Where things stand

**Already landed since round 2 was planned:** #196 completed pi's event payloads; #186 built the compaction lifecycle on the shared lifecycle base; #217 rewrote the private bus checkpoint; #225 deleted unused delivery and transcript bridges.

**What remains, in three kinds:**

1. **Legacy and compatibility code already on `main`:** 131 markers in 61 modules, including loaders that rename old keys, readable "legacy stores," compatibility accessors, old marker keys, a legacy `broadcast` route, and **legacy paths still running beside their replacements** in `publisher.py`, `input_drain.py`, `supervised_cutover.py` and `wire_log.py`. Surface **L0**.
2. **Raw data handled by hand where a type exists or should:** SQLite rows read by column name (321 reads, 32 modules), 34 positional inserts, hand-written exact key-set checks (31 in 21 modules), JavaScript embedded in Python strings (about 10,000 characters). Surfaces **S12**, **S9**, **S10**.
3. **Child processes supervised eleven different ways,** with detached owners identified by bare PID. Surface **S13**.

Plus the two process surfaces that keep it from coming back: **R0** (the CI ratchet and guards) and **R1** (NRA detectors that can see this kind of debt).

---

## Surfaces

| ID | Surface | Builds | File |
|---|---|---|---|
| **L0** | Legacy sweep: delete every compatibility path, converter and legacy route; end every dual path with one path | nothing | [L0-legacy-sweep.md](L0-legacy-sweep.md) |
| **R0** | CI ratchet and guards, as the required check | nothing | [R0-R1-stop-the-inflow.md](R0-R1-stop-the-inflow.md) |
| **R1** | NRA detectors for raw record handling | (in NRA) | [R0-R1-stop-the-inflow.md](R0-R1-stop-the-inflow.md) |
| **S13** | Child-process supervision | A12 `ChildProcess` | [S13-child-supervision.md](S13-child-supervision.md) |
| **S12** | Typed tables and the private N/K records | A13 `TypedTable` | [S12-typed-tables.md](S12-typed-tables.md) |
| **S10** | pi boundary remainder | nothing | [S10-pi-boundary.md](S10-pi-boundary.md) |
| **S9** | Compaction | A14 `PiHelper` | [S9-compaction.md](S9-compaction.md) |

---

## Order

| Step | In parallel | Why |
|---|---|---|
| **1** | R0, R1, **S13 building A12**, **S12 building A13**, **L0 part A** | R0 protects everything after it. A12 and A13 are new modules that collide with nothing. L0 part A deletes compatibility code in files no round-2 surface owns. |
| **2** | S13 and S12 migrating their files, **S10**, **S9** steps that need neither A12 nor A13, **L0 part B** | Disjoint files. L0 part B ends the dual paths. |
| **3** | **S9** adopting A12 and A13 | Needs both. |

A surface's guards go into the required check as soon as it merges, so nothing it removed can come back.

---

## Crossings

| Shared thing | Resolved by |
|---|---|
| Child processes in S9's and S10's files | S13 builds A12 and migrates only its own files; S9 and S10 adopt A12 in theirs |
| `native_runtime_inputs`, read in eight modules | S12 declares its row type through A13; each owning surface switches its reader; L0 takes any reader no surface owns |
| Legacy code inside S9, S10, S12 and S13's files | Each surface deletes it in its own files (rule 1); L0 takes the rest |
| Toad | Every change to the agent-comms/Toad protocol lands with a lockstep Toad PR |
