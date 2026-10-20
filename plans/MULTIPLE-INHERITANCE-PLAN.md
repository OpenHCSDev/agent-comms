# agent-comms: absorbing the remaining slop through multiple inheritance

Audited head: `origin/main` on 2026-10-20 at 17:56 EDT. Scope: all of `src/agent_comms` (431 modules, 4,080 classes), `src/agent_comms_hooks` and `tools`, scanned by AST with Python 3.14, findings checked in the code.

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
- **Threshold three.** A mixin needs three or more members; two copies stay.
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
