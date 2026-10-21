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

## Deletion targets per batch, measured 2026-10-21 on `a40e7f9cd`

Scan: `src/agent_comms`, 426 modules, 0 parse omissions, Python 3.14 AST, 10,936 callables (10,581 methods). Bodies are compared after erasing identifiers, literals and attribute targets: exact shapes, and near shapes at a similarity ratio of 0.8 (and a looser 0.75 pass). A group is **certain** when every copy answers the same required question in every reachable state; the rest are marked uncertain and need the reader traced before deletion.

**The 1,518 above is wrong, and it is wrong in the direction that hides 1D.** Erasing literals makes every method that only returns a literal dict or tuple look identical: `cohort_schema.py:88` and `coordination_tables/assignments.py:320` are both `trigger_bodies` returning a dict of SQL, so they "match", yet their SQL guards different tables and columns. Moving such a body to a common parent deletes nothing, because each member still owns its text. Excluding bodies that only return a literal, real copied behaviour is small: **8 exact groups, 91 removable lines (58 certain)**, plus 3 near groups. Near-duplicates at 0.8 add 9 lines; at 0.75, 57 (overlapping the exact `__init__` group). Parallel class families with the same method names already have distinct bodies; the bodies that agree add 40 lines (the `references` + `trigger_bodies` family, 5 classes, 32; `add_to`/`publish` at `cli_bridges/codex_rollout.py:256` and `runtime_updates.py:355`, 7). So near-similar bodies do not raise the number. Declarations do.

| Batch | Measured target (net `src`) | Certain? | Source of the lines |
|---|---|---|---|
| 1A lifecycle | -97 | yes | 85 flag lines (`terminal` 22, `failed` 10, `active` 10, `complete`/`successful`/`pending` 7, `unresolved`/`executing` 6, `deferred`/`completed` 5) less one 2-line capability per flag, plus 32 empty `successors()` bodies `Terminal` answers |
| 1B codec | -34 | yes | 44 flag lines (`accepts_null` 11 at `acp_extension.py:163`, `accepts_prompt` 11 at `pi_events.py:143`, `opaque` 9, `external_json_shape` 7, `strict_fields` 6) less capabilities |
| 1C small families | -83 | yes | 167 flag lines over 42 names at three or more copies (`multiple_targets` 8, `input_uncertain` 8 read nowhere, `preengagement_target`/`waiting`/`visible` 6, ...) less capabilities; `without_rowid` (16) goes to 2A |
| 1D triggers | -800 | no: needs the stored triggers rewritten | 29 methods, 930 lines, 80 triggers (list below) |
| 1E duplicates | -91 to -150 | 58 lines certain | 8 exact and 3 near groups (list below) |

Batch 1 as landed so far is **+13** (+307 -294). Against this table it should have been about -214 for 1A to 1C alone.

**1D needs one decision, taken here: the stored triggers are rewritten, not reproduced.** `RowMutation.abort_trigger` (`typed_table.py:661`) already derives a no-update and a no-delete trigger per table for `AppendOnlyTables` (`typed_table.py:670`). The 45 hand-written update and delete guards below restate that mechanism per table with their own message text and whitespace (IMPL-13). Rendering today's bytes exactly keeps every message and every indent as capability input, which is the same text moved, not deleted. A trigger holds no data, so `DROP TRIGGER` and `CREATE TRIGGER` from the derived declaration is lossless: one tool in `tools/cutover/` rewrites the triggers of the stored coordination files at install, and `schema_differs` (`typed_table.py:635`) then compares against the derived text. The byte-identical rule above stays in force for tables and columns. Kinds, from the trigger text: 18 refuse DELETE, 27 guard UPDATE (whole row, `UPDATE OF` one column, or a `WHEN` monotonic test), 9 freeze several columns at once (`NEW.x != OLD.x OR ...`, 63 such terms), 13 require a parent row on INSERT, 6 others, and 7 methods compute their text.

1D list (every trigger method in `src`):

| Method | Class | Lines | Triggers by kind |
|---|---|---|---|
| `cohort_schema.py:88` `trigger_bodies` | `ClaimBatchAcknowledgements` | 105 | immutable-columns 1, no-delete 1, no-update 1, other-insert 1 |
| `cohort_schema.py:220` `trigger_bodies` | `ClaimBatchMembers` | 40 | insert-membership 1, no-delete 1, no-update 1 |
| `cohort_schema.py:323` `trigger_bodies` | `CohortDeliveryReceipts` | 50 | insert-membership 2, no-delete 1, no-update 1 |
| `cohort_schema.py:451` `trigger_bodies` | `AwarenessClaimGenerations` | 39 | insert-membership 1, no-delete 1, no-update 1 |
| `coordination_response.py:73` `trigger_bodies` | `ResponseTable` | 7 | computed 1 |
| `coordination_tables/assignments.py:245` `trigger_bodies` | `WakeAssignment` | 47 | immutable-columns 1, no-delete 1 |
| `coordination_tables/assignments.py:320` `trigger_bodies` | `ExecutionAssignmentLink` | 42 | insert-membership 2, no-delete 1, no-update 1 |
| `coordination_tables/attempts.py:264` `trigger_bodies` | `AttemptRecord` | 70 | immutable-columns 1, insert-membership 1, no-delete 1, no-update 1 |
| `coordination_tables/attempts.py:461` `trigger_bodies` | `ReplayAssessments` | 45 | immutable-columns 1, insert-membership 1, no-delete 1, no-update 1 |
| `coordination_tables/executions.py:148` `trigger_bodies` | `CurrentExecutions` | 21 | immutable-columns 1, insert-membership 1, no-delete 1 |
| `coordination_tables/executions.py:400` `trigger_bodies` | `ExecutionRecord` | 73 | immutable-columns 1, no-delete 1, no-update 5 |
| `coordination_tables/participants.py:31` `trigger_bodies` | `Participants` | 25 | no-delete 1, no-update 2 |
| `coordination_tables/participants.py:69` `trigger_bodies` | `ParticipantAliases` | 17 | no-delete 1, no-update 1 |
| `coordination_tables/participants.py:110` `trigger_bodies` | `OwnerGenerations` | 20 | immutable-columns 1, no-delete 1 |
| `coordination_tables/publications.py:129` `trigger_bodies` | `PublicationIntents` | 36 | insert-membership 1, no-delete 1, no-update 1, other-insert 1 |
| `coordination_tables/publications.py:239` `trigger_bodies` | `PublicationEvidence` | 36 | insert-membership 1, no-delete 1, no-update 1 |
| `coordination_tables/recovery.py:64` `trigger_bodies` | `ConnectivityFacet` | 18 | immutable-columns 1, no-delete 1 |
| `coordination_tables/recovery.py:125` `trigger_bodies` | `RecoveryAudit` | 17 | no-delete 1, no-update 1 |
| `coordination_tables/responses.py:153` `trigger_bodies` | `ResponseObligation` | 2 | joins the three below |
| `coordination_tables/responses.py:157` `_retry_trigger_bodies` | `ResponseObligation` | 23 | insert-membership 1, no-update 1 |
| `coordination_tables/responses.py:182` `_lifecycle_trigger_bodies` | `ResponseObligation` | 44 | immutable-columns 1, insert-membership 1, no-delete 1, no-update 3 |
| `coordination_tables/responses.py:228` `_publication_trigger_bodies` | `ResponseObligation` | 54 | no-update 5, other-insert 1 |
| `native_pi.py:214` `trigger_bodies` | `NativeContextJournal` | 16 | computed 1 |
| `native_prompt_binding.py:90` `trigger_bodies` | `PromptBinding` | 7 | computed 1 |
| `native_runtime_input.py:305` `trigger_bodies` | `NativeRuntimeInput` | 25 | computed 1 |
| `native_runtime_input.py:391` `trigger_bodies` | `CurrentNativeCursor` | 15 | computed 1 |
| `private_sidecar.py:210` `trigger_bodies` | `SnapshotMeta` | 7 | computed 1 |
| `triage_native_sources.py:32` `trigger_bodies` | `TriageNativeSources` | 26 | computed 1 |

1E list (paths under `src/agent_comms/`; "removable" is the copies' lines less one owner):

| Group | Copies | Removable | Certain | Sites |
|---|---|---|---|---|
| `__init__` exact | 5 | 12 | yes | `attempt_store.py:47`, `cli_bridges/opencode.py:170`, `cli_bridges/shared.py:660`, `external_transcript.py:545`, `recovery_reader.py:29` |
| `captured_owner`/`owner_identity` exact | 4 | 12 | no | `cohort_schema.py:397`, `coordination_tables/attempts.py:131`, `native_input_record.py:421`, `participant_store.py:49` |
| `trigger_bodies` exact (computed) | 3 | 12 | yes | `coordination_response.py:73`, `native_prompt_binding.py:90`, `private_sidecar.py:210` |
| `__init__` exact | 4 | 12 | yes | `durable_turn.py:34`, `execution_store.py:40`, `exporting.py:425`, `publisher.py:74` |
| `wire_member` exact | 4 | 12 | yes | `pi_payloads.py:450`, `pi_payloads.py:708`, `pi_payloads.py:836`, `pi_payloads.py:1041` |
| `_assert_response_schema`/`assert_cohort_schema`/`assert_native_runtime_schema` exact | 3 | 12 | no | `cohort_schema.py:492`, `coordinated_runtime_schema.py:14`, `coordination_response.py:238` |
| `references` exact | 3 | 10 | yes | `coordination_tables/attempts.py:453`, `coordination_tables/recovery.py:56`, `coordination_tables/responses.py:145` |
| `annotation_disclosure`/`disclosure_for` exact | 4 | 9 | no | `pi_payloads.py:859`, `turn_context.py:882`, `turn_context.py:983`, `turn_context.py:1037` |
| `__init__` near (0.75) | 14 | 39 | no | `agent_event_updates.py:37`, `channel_management.py:130`, `child_process.py:1908`, `durable_turn.py:34`, `errors.py:50`, `execution_store.py:40`, `exporting.py:425`, `installer/upgrade.py:171`, `messaging.py:35`, `owner_lifecycle.py:267`, `owner_restart.py:197`, `publisher.py:74`, `thread_management.py:222`, `transcripts.py:465` |
| `parse` near (0.75) | 3 | 9 | no | `cli_native_package.py:35`, `commander/order.py:787`, `connector.py:807` |
| `apply` near (0.75) | 4 | 9 | no | `todos.py:2406`, `todos.py:2507`, `todos.py:2669`, `todos.py:2745` |

What this leaves for the -2,500 floor: 1A to 1E measure about -1,100 to -1,160, of which about -270 is certain today and -800 waits on the trigger rewrite. The rest has to come from the fixed-value methods row (443 methods; turning `def x(cls): return V` into a class attribute deletes at most 1,190 of its 1,633 lines, and only where the value is a per-member fact rather than behaviour) and from the virtual bus deleting whole modules. Field directives (`WireName(`, `WireOmitDefault`, `Column(` in `Annotated`, 629 lines) are already classes; batch 2 deletes only the repeated groups, not the directives.

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
