# agent-comms: absorbing the remaining slop through multiple inheritance

Audited head: `origin/main` on 2026-10-20 at 17:56 EDT. Scope: all of `src/agent_comms` (431 modules, 4,080 classes), `src/agent_comms_hooks` and `tools`, scanned by AST with Python 3.14, findings checked in the code.

## Correction, 2026-10-21 14:20 UTC: this plan is a deletion plan

The scan below undercounted. It looked only for flag declarations and byte-identical method bodies, found "only 2 groups", and concluded behaviour is already shared well. That set the plan's ceiling at a few hundred lines, and batch 1's first drafts came out net zero in `src` (#2450 +106 -97, #2451 +145 -146, #2447 +56 -51): flags swapped for bases, nothing deleted. Multiple inheritance factors a repeated fact into one owner, so the whole point is deletion. Measured on `main` at `bac74b1a` with Python 3.14 (AST, `src/agent_comms`, 124K lines, 4,471 classes):

| Slop | Size | Owner it moves to |
|---|---|---|
| Class-level boolean flags (`x = True` / `x: ClassVar[bool] = False`) | 500 lines, 175 of them `ClassVar[bool]` | one capability class per flag, carrying the reader's branch |
| Structurally identical method bodies after normalizing names and literals, 3+ copies | 29 groups, 1,518 removable lines | the common base each group's classes share or should share |
| ...of which hand-written SQL trigger text (`trigger_bodies`, `_retry_trigger_bodies`, `_lifecycle_trigger_bodies`) | about 900 lines in `cohort_schema.py`, `coordination_tables/*`, `coordination_response.py` | table capabilities (`AppendOnly`, `Revisioned`, `Retried`, `LifecycleGuarded`) that derive their triggers from the table's columns, the same owner the virtual bus uses for `AppendOnly` |
| Methods whose whole body returns a fixed value, under names repeated 3+ times | 443 methods, 1,633 lines (`successors` 50, `violated` 32, `matches` 22, `trigger_bodies` 17, `title` 13) | a class attribute or capability where the value is a per-member fact; deleted where a base already answers it (`Terminal` answers `successors() == ()`) |
| Lifecycle state machines written once per family | 12 families, 61 `successors` definitions, about 3,500 lines in the `*_states` modules | one `LifecycleState` machinery with capabilities; each family keeps only its members' edges |

Floor for the whole plan: **net -2,500 lines in `src`**. The audit fork raises this per batch once it has measured near-similar bodies (same shape, small differences), which this table does not count.

Rules added to every change:
- **Every PR states its net `src` lines in its body. A net-positive inheritance PR goes back.** New capability classes are paid for by the copies they delete.
- **Delete first, rewire after** (Tristan, 2026-10-21: "I don't care if you delete slop and cause an error because we didn't rewire something, we'll just rewire it. There's never an excuse for slop."). A deletion that breaks a caller is fixed by moving the caller onto the owner, never by restoring the copy.
- **Hunt, don't just execute.** Each unit reports any slop it finds outside its list in #comms with file:line, and the commander assigns it the same tick.

Batch 1 gains two surfaces, run in parallel with 1A to 1C on disjoint files:
- **1D, table triggers.** Every `trigger_bodies`-style method is deleted; each table inherits the capabilities whose triggers it needs, and `TypedTable` derives the DDL. Stored schema text must stay byte-identical (see the rule below), so each capability renders exactly today's trigger SQL; where two tables' triggers differ only by table and column names, that difference is the capability's input.
- **1E, the 29 structural duplicate groups** outside triggers, each moved to its common owner.

## What the scan found

Behaviour is already shared well: only 2 groups of identical method bodies exist across unrelated classes. The slop sits in declarations, where the same fact is written once per class:

| Kind | Size | Example |
|---|---|---|
| Boolean capability flags | 55 flags, 303 declarations | `terminal = True` in 23 classes across five lifecycle families; `without_rowid = True` in 16 tables; `accepts_null` in 12 codec classes |
| Field directives as string keys | about 530 | `metadata={"sql": …}`, `"wire_name"`, `"wire_omit_default"` |
| Field groups declared together | 10 groups in 6 to 11 classes each | `sender` + `target` (11), `turn_id` + `item_id` (10), `owner_thread` + `owner_generation` (9), `offset` + `length` (7) |
| Identical field declarations, metadata included | 24 declarations, 83 copies | `singleton: Literal[1] = field(metadata={'sql': Column(primary_key=True)})` in 8 tables |
| `isinstance` against our own classes | 101 checks on 69 classes | `TextContent`, `SessionEntry`, `DeliveredProblemOrder` |

## The mechanism already exists

Nothing new is invented. Three owners in the tree carry all of it:

- **`DeclaredFamily.members_with(capability)`** (`declared_family.py`, 100 call sites) derives any set of members from a capability class. A capability is a class members inherit; every set that needs it is derived.
- **`MroDispatch` with `@handles`** (`mro_dispatch.py`) runs handlers along the value's MRO, so a consumer reacts to a capability without testing for it.
- **`FieldCodec`** and **`TypedTable`** own encoding and storage, and are where field directives and table capabilities get read.

Multiple inheritance earns its place exactly where capabilities overlap without nesting: an assignment state can be terminal and failed, a table can be a singleton and without rowid, a codec member can accept null and keep an external JSON shape. Each capability becomes one class; members combine them in their bases; the flag lines are deleted.

## Rules for every change

- **A capability class carries meaning, not a boolean.** The one reader that branched on a flag moves its behaviour onto the capability, as a method or an `@handles` handler. A capability that only replaces `x.flag` with `isinstance(x, Flag)` is a rename and is not done.
- **Sets come from `members_with`.** No tuple of member names survives.
- **Stored schema text stays byte-identical.** `TypedTable` checks stored tables by exact SQL text (`typed_table.py:462-472`) and the schema digests hash that text. A field-group mixin may only touch a table if every affected table's `ddl()` output is unchanged; that comparison is the proof for those surfaces. Wire names derive from field names, so renaming nothing keeps every stored record decodable.
- **External shapes are left alone.** JSON a contract leaves open (the shared ledger), Python AST nodes, `typing` objects and SDK types are not ours to wrap.
- **Threshold three.** A mixin needs three or more members; two copies stay, unless both copies are deleted outright.
- **Codemods, one sweep per axis.** The declaration rewrites are mechanical and go through `libcst` scripts in one commit each, with the reader changes in the same PR.

## The plan: four batches, each landing as one

### Batch 1, in parallel (three units, disjoint files)

**1A. Lifecycle capabilities across families.** Owner: a new `lifecycle_capability.py` beside `assignment_states.py`. 93 flag declarations spread over the lifecycle families say the same few things: `assignment_states.py` (12), `obligation_states.py` (11), `execution_states.py` (9), `goal_generation.py` (5), `attempt_states.py`, `goal_states.py`, `pi_vocabulary.py`, `input_attempt.py`, `turn_lease.py`, `native_tool_call.py`, `todos.py` and `bus_route_counts.py` (3 to 4 each), declaring `terminal`, `failed`, `successful`, `pending`, `active`, `executing`, `unresolved`, `deferred` and `complete(d)`, defaulted on each root and overridden per member. Each becomes one capability class shared across all of them: `Terminal`, `Failed`, `Successful`, `Pending`, `Active`, `Executing`, `Unresolved`, `Deferred`, `Complete`. Members inherit the ones that apply (`class FailedAssignment(AssignmentState, Terminal, Failed)`), the roots stop declaring defaults, and readers such as `attempt_store.py:157` (`if phase.terminal`) ask the capability for the behaviour they branch on. Transition rules that today say "terminal states have no successors" move onto `Terminal` once.

**1B. Codec capabilities.** Owner: `FieldCodec`. `accepts_null` (12), `opaque` (9), `external_json_shape` (7), `strict_fields` (7) and `accepts_prompt` (11) are each read in exactly one place; that place's branch moves onto `AcceptsNull`, `Opaque`, `ExternalJsonShape`, `StrictFields` and `AcceptsPrompt`, dispatched through `MroDispatch`. `wire_tag = 'text'` in five classes becomes a `TextWire` mixin that owns the tag.

**1C. Command and small family capabilities.** `multiple_targets` (8 CLI commands), `preengagement_target` (6), `notification_changed` (5), `visible` (6) and the remaining flags with three or more declarations, each moved to the family that owns it. Flags read nowhere (`input_uncertain`, declared 8 times, read 0 times) are deleted, along with the two dead modules from the earlier plan (`acp_ingress.py`, `cli_bridges/opencode_store.py`).

### Batch 2, one owner (one codemod sweep over field declarations)

**2A. Field directives become classes, and repeated field groups become mixins, in the same pass.** Both edit the same field lines, so one owner does both in one sweep instead of two PRs fighting over 500 lines.

- The directive vocabulary becomes classes owned by their readers: `FieldCodec` declares `WireName`, `WireOmitDefault`, `WireRequired`, `SnapshotExclude`, `ContentExclude`; `TypedTable` declares `Column`; the CLI declares its parser directive. Each class carries what it does to encoding, storage or parsing, and the readers ask it instead of branching on a key. Fields attach them through `Annotated`.
- Field groups become record mixins on wire and in-memory records: `Addressed` (`sender`, `target`, 11), `TurnItem` (`turn_id`, `item_id`, 10), `OwnerIncarnation` (`owner_thread`, `owner_generation`, 9, with their checks declared once), `ByteSpan` (`offset`, `length`, 7), `WirePosition` (`wire_root_id`, `wire_seq`, 6), `MessageWire` (`message_id`, `wire_seq`, 6), `ClaimScoped` (`claim_scope` with its wire name, 6).
- The virtual bus (`plans/VIRTUAL-BUS-PLAN.md`) declares `Addressed` as a bus capability, and this batch uses that class rather than declaring a second one. The bus members of `ByteSpan`, `WirePosition` and `MessageWire` are byte positions or `wire_*` names, which the virtual bus deletes, so they are deleted there, not made into mixins here. Only members that survive the virtual bus count toward the threshold of three.
- Table mixins only where `ddl()` is unchanged: `SingletonRow` (the singleton primary key, 8 tables), `SchemaMeta` (singleton plus `ddl_digest`, 6), `Revisioned` (the positive revision check, 4), `ExecutionKeyed` (4), `WithoutRowid` (16 tables). Each table's `ddl()` before and after is compared byte for byte; any table where a mixin would move a column keeps its own declaration.

### Batch 3, by consumer (parallel)

**3A. `isinstance` against our own classes.** 101 checks on 69 classes, read per consumer after batches 1 and 2 have settled the families. Each becomes a method on the class or an `@handles` handler. Checks against external types (AST, `typing`, SDK classes) are excluded.

### Batch 4, cleanup

Any capability or mixin left with one member is folded back into it; any reader still branching on a former flag is fixed. Census at the new head confirms.

## Why this is fast

| Batch | PRs | Units | Stored data touched | Proof at the end |
|---|---|---|---|---|
| 1 | 3, landed as one batch | 3 in parallel | none | the readers' workflows run; full suite |
| 2 | 1 | 1, codemod | none; `ddl()` byte-identical | `ddl()` comparison for every table; every stored record round-trips |
| 3 | one per consumer, landed as one batch | as many as consumers | none | full suite |
| 4 | 1 | 1 | none | census |

No migrations, no carry steps and no extra install rehearsals: nothing stored changes shape. The large edits are codemods, so review reads the codemod and the readers, not 500 hand-edited lines.

## Done means

One capability class or mixin owns each fact; every member that has it inherits it; every reader that branched on it now asks the owner; the replaced flags, rosters, string keys and repeated field lines are deleted with no alias left; stored schemas and records are unchanged; and the actual workflows (installs, order lifecycles, delivery, the CLI) still work. The census then confirms the drop.
