# agent-comms: remaining slop, its owners, and how to remove it

Audited head: `39082ce7c` (main, 2026-10-20 15:34 UTC). Measured with the refactor-audit census and overlay under Python 3.14, every finding below checked in the code.

## Where the codebase stands

Production code grew 45% in a week (76,580 to 111,304 lines under `src/`) and still got cleaner. Since `9fd3e0f29` (Oct 13): `type(x) is` checks −85, `None` identity checks −382, string-key subscripts −85, literal `.get("key")` −68, long boolean chains −31, absence probes −84, while classes rose by 1,813 and abstract bases by 112. Two measures went the wrong way: flattened families +20 and string dispatch +2.

What remains is concentrated. Every item below is a fact with two owners; the section says which owner keeps it and what gets deleted.

## How to execute without churn

- **No stored-data changes unless named.** Nothing in waves 0 to 2 changes a stored format, so nothing needs a carry step, a migration or extra install rehearsal. Each surface is one PR through the normal batch.
- **Mechanical rewrites go through one codemod, not by hand.** Wave 1's field directives are about 500 sites of the same shape; a script rewrites them in one commit, and the readers change in the same PR.
- **Delete first.** Wave 0 is deletions only and goes in one PR.
- **One owner per surface, disjoint files, run in parallel.** Where two surfaces touch one file, the table under each wave says which goes first.
- **Guards are the existing ratchet.** Each surface adds its measure to `tools/debt_ratchet.py` and drives it to zero. No new CI.
- **External formats are honoured exactly.** Claude Code transcripts, MCP content blocks, OAuth discovery paths, JSON Schema keywords and SQLite are other people's contracts: decode them once at entry into a typed record and never pass the dict further. Formats where we own both ends (the Toad wire, bus records) change freely, in lockstep, in one PR.

## Wave 0: delete (one PR, under an hour)

**0.1 The rules archive.** `src/agent_comms/commander/rules-archive/` holds verbatim copies of the 63 rule files that `rules/` replaced, "kept for history". Git already keeps history, so every rule now exists twice in the tree, and agents read the tree. Owner: `commander/rules/`. Delete the directory.

**0.2 The repository as a notebook.** Fleet state and run output are committed as documents: `docs/agents/fleet-board.md`, `docs/agents/fleet-narrative.md`, `docs/agents/handoff-20261018-1830.md`, `FOUNDATION-S6-HANDOFF.md`, `P520-HANDOFF.md`, `PR-BODY.md` at the root, and 78 log, stdout, stderr and `results.json` files under `docs/refactor/` (180 files in that tree). The board and its history are owned by Core; a PR description is owned by its PR; run output is owned by the run. Delete them, and add a ratchet that refuses board, narrative, handoff and log files anywhere in the repository.

**0.3 Dead modules.** Done: `acp_ingress.py` (164 lines) is deleted. Its only importer was Toad (`toad/acp/agent_process.py` and `agent_session.py`), which held Core's route lock and maintenance gate around its own spawn and prompt pipe write. The first deletion searched Core alone and broke `toad-comms` at import. OpenHCSDev/toad#1120 removes that dependency, because the agent-comms-acp child already admits each prompt under `MaintenanceBarrier.ingress_async` (`prompt_state.py:71`). `cli_bridges/opencode_store.py` is live: `History` discovers the `agent_comms.cli_bridges` package (cli_bridges/history.py:35), so its `OpenCodeExport` and `OpenCodeDatabase` are OpenCode's offline history readers. (The overlay also flagged `commander/judge.py`, `commander/rules.py`, `commander/tool_watch.py`, the parity rows and two context segments; those are live, loaded by name through `commander/cli.py`'s package import and by declared-family discovery.)

**0.4 Transition scaffolding.** `commander_parity/` (+1,414 lines this week), `tools/carry_rts_orders.py` and `tools/cutover/rts-launchers.patch` exist only for the switch. Each gets a docstring naming its deletion condition now ("delete when both machines run the native commander"), and is deleted that day.

## Wave 1: shared owners (three PRs in parallel)

**1.1 Field directives are strings.** About 500 dataclass fields carry codec and storage instructions as string keys: `metadata={"sql": …}` 218 times, `"wire_name"` 138, `"wire_omit_default"` 81, `"wire_required"` 12, `"registration_inheritance"` 12, `"snapshot_exclude"` 9, `"content_exclude"` 8, `"thread_slice"` 6, and `field_codec.add_argument` reads `flags`, `group`, `parser_default`, `parser_default_factory`, `parser_options` the same way. The values are mostly typed already (`"sql"` maps to a `Column`); the vocabulary is the restated fact, spelled at every field and again in each reader (`typed_table.py:359-364`, `field_codec.from_declaration`, `field_codec.add_argument`).

Owner: each directive is its own class, and the class is the key. `FieldCodec` declares `WireName`, `WireOmitDefault`, `WireRequired`, `SnapshotExclude`, `ContentExclude`; `TypedTable` declares `Column`; the CLI declares its parser directive. Fields attach them with `Annotated[T, WireName("x"), Column(...)]`, and readers find a directive by its type. A codemod rewrites the 500 sites in one commit; the readers change in the same PR. Wire names and column names are unchanged, so no stored data moves. Guard: zero string keys in field metadata.

**1.2 Decode once at every boundary.** Raw dicts walked by string key after they enter:
- `channel_coding_tools.decode_request` checks `set(value) == {"token", "call_id", "name", "arguments"}` by hand, a second owner of the shape `cli_bridges/codex_rollout.py::FunctionCallItem` already declares. Decode into that record through `FieldCodec`; delete the key-set check.
- `ui_model/delivery.from_wire` reads `currentScope`, `inputs`, `historicalInputs` and two counts by key. We own both ends: declare the record, change Toad and Core together.
- `cli_bridges/claude_protocol.local_command`, `image_inputs.prompt_images` and `native_package.declared`: external or packaged formats, decoded once into records at entry.
- `bus_page_index.record` reads `from`, `seq`, `to` by key: declare the page record.

Guard: the overlay's raw-shape count at zero outside the codec itself.

**1.3 Two parsers of one command line.** `commander/send_guard.py` dispatches on `{"python": …, "bash": …, "sh": …}`, checks `program.startswith("python")` and reads other processes' environments from `/proc`; `storage_rules.effects` and `storage_rules.decode` separately switch on interpreter flags (`-B -E -I -s -u`, `--reason`, `-b`). Owner: `ShellInvocation`, which already splits commands, gains a declared program family (a Python program with its flag family, a shell program) that both callers use. Delete both string tables.

## Wave 2: core state (parallel, by file)

**2.1 Absence as `None` where absence has behaviour.** 246 absence probes across 112 files, heaviest in `typed_table.py` (9), `backend.py` (9), `input_drain.py` (7), `turn_watchdog.py` (6), `commander/order.py` (6), `attempt_start.py` (6), `todos.py` (5). Where a missing value changes what the code does, the absent case becomes a declared member that carries that behaviour, the pattern the tree already uses in `NoBackup`, `NoOrderDeletion` and `UnplannedPhase`. A plain optional value with no behaviour stays `None`. Do the seven files above first; the remaining 1,094 `None` checks are measured per file and only touched where they decide behaviour.

**2.2 Families flattened at their boundary.** 35 sites, 12 in `typed_table.py` (`member.value` read out of declared enums, string-valued members checked by `isinstance`), then `channels.py` (4), `coordination_schema.py` (3), `tools.py`, `pi_events.py`, `field_codec.py`. The family owns the fact; the boundary turns it into strings and every consumer decides it again. Carry the class across and let `FieldCodec` encode it once.

**2.3 Record shapes inside core code.** `todos.py:999` writes the staged install record as `json.dumps({"at": …, "installer": …})`; `connector.py` (6 key subscripts), `tools.py` (5), `request_progress.py` and `view_unread.py` hold similar shapes. Each becomes a typed record encoded by `FieldCodec`. The staged install file is stored data: it is written and read by the same install, so it is reset, never carried.

**2.4 Switches over types.** `commander/order.py::overrides` and `::own` switch on Python AST node types: use `ast.NodeVisitor` methods, which are the AST's own polymorphism. `shared_ledger.remove_threads` switches on its value's type: give the value family the method.

**2.5 One god function.** `commander/order.py::send` is 146 lines. Its parts belong to the typed order record P290 introduced; move each to the order type that owns it.

Crossing: 1.1 and 2.2 both touch `typed_table.py` and `field_codec.py`, so 1.1 lands first. 2.1, 2.4 and 2.5 all touch `commander/order.py`: one owner takes all three.

## Wave 3: external boundaries, lower priority

- `locked_store._canonical_schema` switches on 12 JSON Schema keywords. The keywords are an external standard; declare them once as a keyword family that owns each keyword's canonical form.
- `pi_events.py`, `pi_payloads.py`, `pi_commands.py` and `native_runtime_input.py` hold most of the remaining string-keyed literals (`pi_events.py` 36, `native_runtime_input.py` 23, `pi_commands.py` 19). Decide whether the Pi runtime's wire is ours or external; either way, decode once at entry and nowhere after.
- Broad `except Exception`: 65 sites, 14 in `child_process.py`. Each either names the error it expects or is deleted.

## Status of waves 0 to 3 on `0bcbe5164` (2026-10-21)

| Item | State | What is left |
|---|---|---|
| 0.1 rules archive | done | |
| 0.2 repo as notebook | partly | 47 JSON proof artifacts and 4 run scripts under `docs/refactor/`, `docs/evidence/cn-phase3-record-results.jsonl`; no ratchet refuses them |
| 0.3 dead modules | done | |
| 0.4 transition scaffolding | partly | deletion conditions are written; `commander_parity` (about 2,600 lines) and `rts-launchers.patch` remain until cutover |
| 1.1 field directives | done | |
| 1.2 decode once | partly | `ui_model/delivery.py:67-78` decodes rows and re-encodes them to dicts for Toad (4.6) |
| 1.3 one command-line parser | partly | `storage_rules.py:608` and `:676` still hold inline flag tables; `TestsOnCommands`/`BuildsOnCommands.commands` restate `TestGateProgram`/`BuildProgram.names` |
| 2.1 absence | partly | 781 `is None` checks; `input_drain.py` 12, `typed_table.py` 11, `backend.py` 9 |
| 2.2 flattened families | partly | `typed_table.py` 7 `.value` reads |
| 2.3 record shapes | done | |
| 2.4 type switches | open | `commander/order.py:135-164` `overrides`/`own`; `shared_ledger.py:41` `remove_threads` |
| 2.5 god function | open | `commander/order.py:395` `send`, 131 lines |
| 3 `_canonical_schema` | superseded | delete it (4.3); its only caller `format_fingerprint` has no caller |
| 3 broad except | mostly done | 7 left (65 before) |

## Audit of 2026-10-21 (`0bcbe5164`): waves 4 to 8

Three audits, one per catalog rung group, each finding read in the code and checked against this plan, `VIRTUAL-BUS-PLAN.md`, `MULTIPLE-INHERITANCE-PLAN.md`, `commander-native-plan.md` and `docs/design/goal-execution-derived.md`. Anything those already own is left out. The rule applied: a fact restated outside its declaration is slop, and anything that could be factored through inheritance and is not, is slop. Line counts are estimated net deletions from `src/`.

### Wave 4: one owner per identity (stored data moves; each item names its carry)

**4.1 A thread's name has four owners (IDEN-5, about 250).** The registry owns it. The coordinator keeps `Participants.display_name` (never read, never updated on rename), `ParticipantAliases` (all four `register` callers pass `(lookup, name, name)`, so it stores the lookup as its alias) and `OwnerGenerations.owner_thread`, copied onto execution, attempt, `NativeRuntimeInput` and `PromptBinding` rows and compared with `!=` at `attempt_start.py:74`, `execution_store.py:113`, `coordination_response.py:149/271`, `selected_claim.py:103`, `acp_extension.py:383`. A rename is therefore a two-store transaction with a durable intent file and a compensating rollback (`PrivateOwnerRename`, `PrivateOwnerRenameIntent`, `_require_no_private_owner_rename` in eight places). Owner: the coordinator keys by lookup; names resolve through `RegistryProvenance`. Delete the three copies, the `owner_thread` columns and checks, `ParticipantStore.rename` (no callers), the rename protocol and the eight gates. Carry: the participant and owner-generation tables. MI batch 2A no longer builds an `OwnerIncarnation` mixin over these fields.

**4.2 Context requests keyed by the thread's current name (IDEN-6, about 15; a bug).** `context_store.py:208-241` keys by `(thread_name, thread_birth)` and `newest(owner)` filters on the name, so after a rename `latest()` returns `AbsentContext` and the turn counter restarts at 1; paging fans out over every alias to compensate. Key by the incarnation lookup and delete the fan-out. Carry: the context request table.

**4.3 One incarnation, four encodings (IDEN-3, about 40).** `RecordedThreadIncarnation`, a 32-hex lookup, a bare `created_at` float (`goal_history.py:53`, `acp_extension.py:221/272`) and `(name, created_at)` pairs rebuilt and re-validated in `goals.py:165-185` and `goal_failure_observation.py:46-76`. One field typed `RecordedThreadIncarnation`, stored by its lookup.

**4.4 Turn state in four places (IDEN-5, about 20).** `turn_record`, `turn_generation` (0 meaning none), `last_finished_turn_id: str | None` and `ActiveTurn.turn_generation` (`threads.py:205-208/252/468/472`, `registry_store.py:156-167`, `turn_lease.py:203-209`). A `FinishedTurn(id)` member of the turn-record family; one owner for the generation.

**4.5 A turn's owner is a bare PID (IDEN-8, about 5; a bug).** `ActiveTurn.owner_pid` (`turn_lease.py:204/273-279`), compared by PID alone in `child_process.py:263` `owns_turn`, so a reused PID owns a stale turn. Store `ProcessIdentity` (pid plus start_time) and compare identity, as the goal-execution design does for attempts.

**4.6 Half-finished renames between code and our own wire (IDEN-2, about 25).** "claim" on the wire and in the model's context means an assignment (`assignment_id` written as `claim_id`/`wake_claim_id`, `context_segments/wake.py:87`), while `claim_*` in code means the admission scope (`claim_scope` written as `admission` at 11 sites, `claimed_generation` as `admission_generation`, `claim_epoch` as `admission_epoch`). We own both ends: wire keys equal field names, the rows are carried, about 25 `WireName` overrides go. Same wave: `InputDelivery` repeats `DeliveryOverview` and re-encodes rows for Toad (`ui_model/delivery.py:38-78`); it holds the overview, Toad changes in lockstep.

**4.7 Format identity has three owners (TIME-2/4/6, about 360).** (a) `format_fingerprint`, `_canonical_schema` and `DocumentConversion.source_fingerprints` (`locked_store.py:295-337`) are called by nothing; their manifest test is gone and `tests/guards/stored_format_fingerprints.json` is orphaned. (b) `RegistryGoalConversion` and `convert_format` (`registry_store.py:181-262`) convert version 0 to 1 and only tests call them; production raises "run convert_format during installation" and no installer step does. (c) `UncarriedRecordFormat` (`debt_ratchet.py:1665-1731`) with `tools/stored_formats.py` enforces hand-bumped `version` stamps, carried on about 16 records between our own writer and reader (`active_route.py:102-140` merges its version outside FieldCodec; also `envelope_claim_transitions.py:223`, `wake_candidate_index.py:87`, `audience_manifest.py:89/123/192`, `maintenance_barrier.py:199`, `coordination_snapshot.py:59`, `private_registry_guard.py:43/343`, `registry_document.py:254`, `relationships.py:520`, `delivery_policy.py:35`, `pi_payloads.py:1629`, `native_package.py:278`, `view_unread.py:37`, `exporting.py:504`). Owner: the FieldCodec-derived record schema. Delete (a), (b), the stamps and constants; the ratchet compares derived schemas and requires a carry that names the record.

**4.8 The delivery manifest declared three times (TIME-8, about 40).** `Delivered`, `CommittedDelivery` (which does not inherit it) and `DeliveryManifest` (`delivery_policy.py:19-30`, `bus_publication.py:61-78`, `cohort_schema.py:72-75`, `coordination_tables/assignments.py:123-126`); `decisions` is `list[dict[str,str]]` in one and typed in another. The other two inherit `Delivered`; `resolver_version`/`policy_version` go everywhere, not only on the bus row the virtual bus names.

### Wave 5: owners that exist and are bypassed (code only)

**5.1 Directory fsync and atomic replace (IMPL-13, 110-150).** `private_path.fsync_directory` exists and about 16 sites re-implement it with drift (O_NOFOLLOW present or not, O_DIRECTORY missing at `exporting.py:558`, `os.name` gating present or not): `diagnostics.py:278`, `native_package.py:364`, `goal_history.py:133`, `goal_attempts.py:755`, `installer/backup.py:215/328/367`, `installer/carry.py:168`, `native_session_files.py:97`, `active_route.py:248/448`, `locked_store.py:100-129`, `harness_install.py:37`, `restart_queue.py:221`, `private_registry_guard.py:289`, `store_files.py:519`. `goal_attempts._sync_paths` and `goal_history._sync_file` are one function with the comment copied. Every site calls the owner; every atomic replace goes through one `AtomicFile`; the `fsync_parent: bool` flag goes.

**5.2 OS facts outside `Platform` (IMPL-13, 40-60).** 27 `os.name` tests, including the private-mode checks at `goal_history.py:149`, `goal_attempts.py:681/715/720`, `coordination_database.py:299`, `maintenance_barrier.py:300-301`. `Platform` (`child_process.py:418`, Posix/Linux/Darwin/Win32) owns them: `sync_directory`, `require_private_mode`. `/proc` is parsed by hand five ways (`child_process.py:537`, `storage_rules.py:198-227`, `storage_room.py:147-158`, `owner_launch.py:40`, `owner_lifecycle.py:107`, `active_route.py:331`, `host_services/__init__.py:144`, `tools/agent_resource_check.py:37-90`): one procfs module of records (`ProcStat`, `ProcStatus`, `MemInfo`) behind `ProcessIdentity.arguments()`/`.environment()`.

**5.3 Content digests (IMPL-13, 60-90; a bug).** About 60 inline `hashlib.sha256` calls bypass `TextDigest`/`Sha256Text`, and dict digests disagree on `sort_keys` (`diagnostics.py:245`, `turn_context.py:1206` with; `partner_activity.py:56`, `relationships.py:22`, `turn_context.py:1104/1135` without), so equal content can digest differently. A `ContentDigest` with `of_bytes`, `of_record` (canonical encoding fixed once) and `matches`; fields typed by it.

**5.4 Hand-written dispatch where the mechanism exists (IMPL-13).** `TurnInboxReceiver(Protocol)` plus five `deliver_to` double-dispatch methods (`agent_backend.py:139-246`) become `TurnInputs(MroDispatch)` with `@handles`. The unknown-member fallback is written nine times as try/except (`pi_commands.py:118/126`, `pi_events.py:124`, `pi_payloads.py:306/574`, `pi_vocabulary.py:46/196`, `native_entries.py:118`, `tool_declaration.py:52`) though `OpenPayload.wire_member` already does it with `members_with(UnknownPayload)`: an `UnknownMember` capability on `DeclaredFamily`. Protocols `WiredValue` (`field_codec.py:780`), `CatalogSelection`, `AppliesOverPrevious` become nominal ABCs. Compact JSON `json.dumps(..., separators=(",", ":"))` at about 35 sites becomes `FieldCodec.compact`. SQL `IN (?,…)` built by hand at 11 sites becomes a `TypedTable` predicate.

**5.5 Family tags renamed by hand at every boundary (IDEN-2, 40-50).** kind to action (owner socket, ours on both ends), kind to type (Pi), kind to `wire_tag` (`runtime_requests.py:71-87`, `pi_commands.py:111-128`, `pi_payloads.py:219-306`), while `family_discriminator` exists and eight families use it. Declare the discriminator on each root and delete the pop/insert code. `wire_tag` means the key on roots and the value on members; 63 of 147 declarations equal the snake-cased class name and derive.

### Wave 6: precedence and kinds belong to the family

**6.1 Hand-numbered precedence in seven families (MRO, about 70; one bug).** `TurnFailure.precedence` 100/80/70/60/50/45/40/30 (`turn_failure.py:22-166`, `supersedes` uses `>=`, so a later failure of equal rank silently wins), `DeliveryProgress.priority` 1/2/3 (`notification_progress.py:17-188`, with `IgnoredProgress` copying `ReadDelivery.priority`), `FoundProgress.precedence` (`todos.py:939-1054`, numbers that contradict the class tree), `ToolRequest.action_order` (`tools.py`), `ACPFailure.classification_priority` (`acp_failure.py:176/362/387`), `NativeInputRecord.proof_order` (`native_input_record.py:231/264`), and `STORE_LOCK_ORDER` (`store_order.py:16-22`; the virtual bus keeps a declared order for non-bus stores, so declare it on the stores). `DeclaredFamily` gains one ordering mechanism (declaration order or nested rank bases) and `first_matching`; every integer and sort key goes.

**6.2 A turn failure declared twice (IMPL-1, IDEN-3, 40-60).** `FailureReason` StrEnum (`diagnostics.py:45`) beside the `TurnFailure` family, joined by `code = FailureReason.X` on eight members, with reason codes also as free strings (`backend.py:161/595`, `GivenReasonCode("...")` at `attempt_store.py:228`, `durable_turn.py:163/169`, `attempt_recovery.py:380/422/496`, `selected_participant.py:98`, `turn_phase.py:71-225` stall reasons, `pi_events.py:369/830/847` retry reasons). `terminal_failure_reason` probes `asdict(Done)` by key, and the terminal and drain diagnostics are hand-built dicts (`diagnostics.py:135-255`) beside a `NativeTerminalFailureRecord` with zero readers that the writer does not even match. `TurnFailure` members are the reason; `Done` carries the member (and splits its `ok: bool` into success and failure members); the documents are encoded records. Delete `FailureReason`, `code`, `terminal_failure_reason` and the dict literals. The goal-execution design's `FailedTurnEvidence.reason` becomes `type[TurnFailure]`.

**6.3 Kinds as strings or bare enums beside their families (IMPL-1/2/7, about 130).**
- `TagAction(Enum)` with `match self` and a parameter bag (`channel_management.py:109-125`): a `TagEdit` family, as the sibling `RelationshipEdit` already is.
- `ExecutionOrigin` StrEnum switched in Python five times and in SQL as `'wire'` four times (`execution_store.py:87-90`, `coordination_tables/executions.py:176/201/300/304/436`, `coordination_snapshot.py:202-206`, `cohort_schema.py:421`): `WireOrigin`/`ClaimlessOrigin` members own `require_sources`, retry eligibility and the SQL guard.
- `goal_history.py` state and kind `Literal`s spelled three times each and compared as strings, plus `RecordedGoalRevision` restating the entry's fields: families, and the entry inherits the record.
- `WhenBusy` Enum with a parallel `{STEER: "steer", FOLLOW_UP: "followUp"}` table, a `Literal`, and a `streaming_behavior: str` parameter (`agent_backend.py:122`, `turn_inputs.py:37`, `pi_commands.py:161`, `private_sidecar.py:383-402`): one family whose members carry their Pi spelling.
- `tools.py` `context = "thread"` tag with `None` branches (`:402-428`, `:1541-1713`): a `ThreadContextAction` capability.
- StrEnums beside `DeclaredFamily` (`wake.py:19` `ControlClassification`, `wake_policy.py:21/31`, `messages.py:152` `MembershipChange`, `pi_vocabulary.py:218` `ServiceTier`, `channels.py:349` `ViewKind`, `activity.py:56-80` `ActivityState`): one family mechanism.
- Projection views named by strings equal to directive class names (`@projected(view="snapshot")` 15 times, `"presence"` 7, `"wire"`/`"content"` 4): a `View` family taken by class.
- `RecoveryCondition` matching a lifecycle by `declared_name` string equality across two families (`recovery_states.py:21-26`): `issubclass`.

**6.4 Rosters and registries beside families (MEMB-1/2/4, about 120).**
- Four remaining hand-rolled `__init_subclass__` registration owners: `Carry.declared` (`installer/carry.py:42-58`), `ThreadRename.kinds` (`thread_management.py`; still present, not removed by 4.1 at this integration base), `BirthlessIncarnation.members` (`thread_identity.py:214-226`), and `_start_phases`/`_end_events` (`pi_events.py:59-93`). Move registration through the existing nominal mechanisms or derive behavior through the MRO; preserve each boundary's actual keys and consumers. `CliToolArguments.types` and its private hook are removed by the CLI argument slice below.
- Preserve the independent Pi producer ABI contract: `PI_0_85_1_EVENT_KINDS` in `pi_native_backend.py` declares the pinned runtime's emitted kinds, while `NativePiEvent.known_kinds()` declares converter coverage. `AgentBackend.require_event_family` compares these independent answers and reports missing or extra converters. Deriving the expected producer kinds from converters would erase this check. Refactor the internal `_start_phases`/`_end_events` registration lifecycle without weakening producer/converter coverage; removal of the external manifest is not a completion condition.
- Backend role slice #2623 is published and reviewed, awaiting integration: it removes `THREAD_CAPABILITIES`, the serving flags, `validate_backends` and copied `unstarted_type` declarations. `ThreadServingBackend(Steers, Compacts, AgentBackend)` puts capability bases first so `AgentBackend`'s concrete refusal method cannot hide the abstract turn obligation; `NewThreadBackend.selected` derives the unique default. Preserve the independent producer/converter event comparison.
- Display names re-typed from class names in about 40 places (`tools.py` labels, `notification_progress.py:45-157`, `todos.py:1502/2012/2088/2605/2675`, `display_order.py:76-136`, `relationships.py:382/397`, `turn_phase.py:169/218`, `config_options.py:192-194`, `input_attempt.py:400/440/447` `public_status`): derived from the declared name, literal only where the text differs.
- Sigils `#`/`@` spelled by hand at about 22 sites (`catalog_document.py:61-232`, `channels.py`, `channel_targets.py`, `goal_presentation.py:71-72`, `goal_actions.py:305/318`, …): owned by `Tag.channel` and `ThreadTarget.mention`.
- Store and file names restated outside their class: `root / "wire"` in about 20 places across 17 modules (the virtual bus plan's done-grep for `_wire_lock_path` misses every `_store_lock(root / "wire")`; its grep gains that pattern), `~/.local/state/agent-comms` six times, bridge inbox dirs, `"registry.json"` at `installer/upgrade.py:566`, `".registry-owner-guard"` at `goal_history.py:208`, `active-route.json` five times, Pi `auth.json` in four modules: `X.path_in(root)` and one state-home owner.

CLI argument slice (#2626, base `acb2ddd0e`, 2026-10-23): the existing
`RegisteredWireMeta` now owns argument-model registration after Pydantic/ABC
construction. Each concrete declaration supplies its genuine external
`cli_names` tuple as the registry key; decode derives matching shapes from
that registry without retaining another alias map. Both production consumers,
Claude `ToolUse.call` and OpenCode `ToolState.call`, keep the shared decode
entrypoint. The seven shapes retain all 13 case-sensitive external spellings,
extra input, canonical previews and edit patches. Unknown names and invalid
arguments remain the original call; validation refusal belongs to its runtime.
Abstract specializations cannot overwrite concrete aliases. The existing
registry rejects duplicate tuples at declaration; overlapping nonidentical
tuples raise an explicit ambiguity at decode, before argument validation can
turn the conflict into an ordinary refused call. Overlap is not checked at
declaration. A new shape still needs only its class and alias declaration;
the improvement is shared declaration lifecycle and collision behavior, not
a claimed reduction from multiple authored rosters.

NRA before/after source receipts cover Core/hooks/tests/tools, paired Toad and
the installed metaclass-registry/Pydantic source: 1,643/1,644 modules,
7,434/7,438 original classes, 7,144 projected classes in each, zero parse
omissions. The 290/294 unprojected originals remain explicit; the four added
classes are function-local declaration controls. Dynamic source projection
does not prove their runtime behavior. The compact index also leaves the
`AutoRegisterMeta` package re-export unresolved as a base of
`RegisteredWireMeta`; source inspection and a fresh-process runtime MRO read
confirm its composition with `ModelMetaclass`. Focused source checks pass 31 cases
(0.63s), exercising real Claude/OpenCode models, edit presentation and actual
fresh declaration lifecycles without patching registry state. The first run
passed 26 and failed two new assertions that incorrectly put call identity on
search preview records; identity is checked on its owning `ToolCall` and
emitted transcript instead. Receipts remain in the owned
`cli-tool-argument-family` scratch directory. Fresh CLI/UI-model import checks
also pass (two cases, 3.73s). This slice is a published draft,
not merged, installed or live-qualified. Shared phase proof belongs to its bus
owner; the other registries and the remaining name/sigil/path work keep 6.4
open.

### Wave 7: records where parameter lists and dicts stand in

- `ManagedLaunchRequest`: nine parameters declared in seven signatures and forwarded six times (`native_child.py:222/341`, `native_pi.py:491/536`, `native_session_prepare.py:67/88`, `backend.py:112`, `stream_agent_events` has 23), with one lock-launch-fence procedure written twice. About 60.
- `TurnRequest`: 12 parameters declared twice and assigned by hand 13 times (`turn_runner.py:441-472`, `owned_turn.py:87-115`); also `TrackedTurn.__init__`/`execute` sharing nine. About 40.
- The Todo revision bump restated by 16 `require_revision` calls and 18 `revision + 1` writes (`todos.py:1607-3320`) and checked again at `:3398`: `TodoStore` checks and bumps once; each operation returns its change. About 35.
- `ProblemOrder`'s nine refusing defaults and duplicated refusal texts (`todos.py:1401-1466/1716/1797`): `UndeliveredOrder`/`DeliveredOrder` capability bases. Same shape: `TurnFailure.retry_delay` and `Platform.parent_identity`/`wait_exited` exist on bases only to refuse.
- Legality raised and caught as an ordinary verdict (`schedule_rules.py:47/67/151`, `goal_scheduler.py:87/98`, `owned_send.py:126/133`): `verdict()` returns `Permitted` or `Refused(rule)`; the identical `schedule` bodies at `:66` and `:150` move to `SessionScheduleCheck`.
- `LockedStore.update` results smuggled out through `nonlocal` (`read_ledger.py:84`, `relationships.py:851`, `shared_ledger.py:76/110`, `input_disposition.py:308/361`, `owner_lifecycle.py:509`): `update` takes a `DocumentChange[T, R]` and returns R.
- `FieldCodec.value_schema` (`field_codec.py:1417-1457`) switches on typing origins that the `ValueDecoder` family already dispatches on; each member owns `schema()`.
- `coordination_cohort.py:88-115` compares an assignment's identity field by field through `getattr` over string tuples: a frozen identity sub-record compared with `==`.
- Copies below the MI threshold of three are still copies: `index_predicate` (`routing.py:184`, `read_basis.py:978`), `require_source` (`turn_context.py:1263/1403`), `execution` (`native_runtime_input.py:136`, `native_prompt_binding.py:74`), `normalize_field` (`pi_payloads.py:432/1162`), `ImportSnapshot` repeating `ImportedSessionMetadata` (`importing.py:43/72`), `ContextCommitted` repeating `NativeContextRecord` fields (`native_pi.py:145`, `pi_events.py:458`), `McpMessage` repeating `McpEnvelope.jsonrpc` and the JSON-RPC error declared three times (`connector.py:237-262/360-430`, `owner_protocol.py:140-146`, `codex_protocol.py:248`). Lifted to the shared base.
- The wake frame written by hand four times (`context_segments/wake.py:85-96/152-157`, `assignment_states.py:376`, `optional_awareness.py:103`) and `WakeDecision` re-implementing `FieldCodec` (`wake_policy.py:105-114`): a declared `WakeFrameSource` record.
- `ConnectivityFacet` table and triggers, both connectivity enums and `observe_connectivity` have no production writer (`coordination_tables/recovery.py:29-75`, `attempt_store.py:235-262`): deleted, and MI 1D does not rewrite their trigger. About 55.

### Wave 8: boundaries, process and tools

- Core-to-Core and Core-to-Toad formats carried as dicts: the owner socket carries ACP pydantic models as `dict[str, object]`, `model_dump`ed, `_meta` popped and re-merged, re-validated on arrival (`owner_protocol.py`, `runtime.py`, `runtime_requests.py`, `acp.py`); the ACP `_meta` extension has six keys each with its own hand encode and `.get` decode, and refusal reasons are bare strings at about 12 `RequestError(..., {"reason": ...})` sites. Typed fields through FieldCodec, one `AgentCommsMeta` record, one `RequestRefusal` family; Toad in lockstep. About 130.
- Pi's pre-decoder (`pi_payloads.py:180-260`, every `normalize_field`/`normalize_wire`, `pi_events.py:112-121/788-806`) rewrites dicts by key before FieldCodec: each quirk becomes a capability FieldCodec consults. About 120.
- `agent_comms_hooks` carries a second codec and a second Claude transcript model (`__init__.py:24-43`, `deferral.py:22-35`) on the claim that hooks import only the standard library, which `session.py` and `skill.py` already break: use FieldCodec and `claude_protocol`. About 60.
- Python embedded as `-c` strings with stdout parsed, about nine times (`installer/runtime_resolution.py`, `installer/upgrade.py`, `installer/backup.py`), and `Stack.cli` polling by substring-matching "Route index covers": one `installer/probes.py` run with `-m`, returning records. About 40.
- Nothing in the stack is pinned: the merge-queue gate `module_layers.py --check` reads the live heads of Toad, Textual and ui-theme (so a Toad push turns Core's queue red, as on 2026-10-21), no `stack/uv.lock` is committed, and CI and the installer run `uv lock --upgrade` before `sync --locked`. Commit the lock, bump it in lockstep PRs, and have the gate and `RuntimePackage` read it. Toad imports about 100 Core modules and the Textual fork imports Core: Toad consumes `ui_model` plus one declared client module, Textual imports nothing from Core, and the gate shrinks to that surface.
- CI: the install line appears four times and checkout/setup seven; Node and uv pinned twice; `test_native_pin` runs twice; publish builds twice; the PR test matrix never runs on PRs; the queue lives in `debt-ratchet.yml`. A setup-core composite action, one build, honest names. The Pi version is restated in CI, the extension's deps and a constant name (`backend.py:93-97`), and its 300 s idle default is copied: read the manifest and write the setting. The pin manifest's meaning lives in comment lines parsed by `startswith` and `sed`: a typed record.
- `tools/module_layers.py` restates the layer table, orders layers by tuple position, ignores `agent_comms_hooks` (so it lists live `behaviour_patterns` as dead), and its committed doc was stale on its commit day; generate the doc in CI, include hooks, enforce or delete the layer rule.
- Dead: `tools/codex_duplicate_execution_hook.py`, its toml, `tools/prove_commander_dispatch.py` (285 lines, unreferenced). `AnnotationPolicy.configured()` reads an environment variable nothing sets, so the working-memory annotation path (about 1,000 lines with `coordination_tables/annotations`) is unreachable in production: declare it in config or delete it (Tristan decides). `AGENT_COMMS_AGENT_MODELS` (tests only), `AGENT_COMMS_DEBUG_LOG` (read five times), `--login` scanned from `sys.argv` beside the parser. `SavedView.original_targets` is migration-only and the retired-layout glob runs on every queue lock. `active_route.py`'s historical-default route and its `guard_original_root_write` at 20 sites, once every machine has published a route. `tests/fixtures/wire_exports` once the virtual bus lands.
- Two plan files differ only in case (`plans/REMAINING-SLOP-PLAN.md`, `plans/remaining-slop-plan.md`) and collide on case-insensitive checkouts; `module_layers.py` cites the lowercase one. One file.

### Order of waves 4 to 8

| Wave | Units | Stored data | First |
|---|---|---|---|
| 4 | one owner each for 4.1-4.3 (one carry), 4.4-4.5, 4.6, 4.7-4.8 | yes, each named | 4.1 before MI batch 2A |
| 5 | by owner module, parallel | none | 5.1 and 5.2 together (`Platform`) |
| 6 | 6.1 one unit (the ordering mechanism), 6.2 one unit, 6.3-6.4 by file | none | 6.1 before 6.2 |
| 7 | by record, parallel | none | |
| 8 | boundaries in lockstep with Toad; process and tools any time | 8's lock file only | the stack lock |

Estimated net deletion from `src/` across waves 4 to 8: about 3,000 lines, plus about 1,000 if the annotation feature is deleted.

## Wave 9: slop found running the fleet on 2026-10-21

Every item was found while installs, reviews and the commander's load failed in front of Tristan. Each is a fact stated in more than one place, a process that grades the wrong thing, or a string standing where a type is the identity. Strings are allowed as attribute values and enum spellings, nowhere else. Fixed items are listed so nobody rebuilds them.

**9.1 The maintenance gate was hand-placed per writer (fixed for stores).** Installs 48 to 50 each refused on the next writer nobody had gated. Fixed: #2594 gives every carried SQLite store the gate through `CarriedWriter`, and #2596 does the same for JSON documents through `CarriedDocument`. Left: `agent_activity.set_activity` and `wire_log` append still call `assert_open_unlocked` by hand. The batch deletes `wire_log`. Activity is a runtime file, so its check is an input-admission rule and moves to the admission owner.

**9.2 The installer identifies files by strings.** `bus_files` keys every file by its relative path string and skips companions by suffix strings (`.lock`, `-wal`, `-shm`, `-journal`). `carried` strips `-wal` by string. Steps declare `Carried.rewritten` as file-name strings, and steps are keyed by module-name strings (`Carry.declared`, `step.name()`). The owner is the RootFile family: a file's identity is its declaring type, its companions come from `SQLiteFile.member_paths`, and a step declares the types it rewrites. An undeclared file is its own `UndeclaredFile(path)` member, never a string. The finished ledger step goes now (#2600).

**9.3 The transcript hold is a workaround (#2592).** It holds one store's lock by comparing a name string. Once #2594 and #2596 are installed, no writer can touch a carried file during maintenance, so #2597 deletes the window hold. `transcripts_held` and its `-c` program string around the backup go too.

**9.4 The merge queue gated nothing.** The ruleset required review-verdict, but an admin bypass with mode "always" covered every agent (all push as trissim). 37 of the last 100 merge groups failed and still merged, and #2582 merged as a draft with review-verdict failing. Rule from Tristan: the queue is the path for every PR, and a bypass is only for a massive backlog he agrees to. The ruleset change is his.

**9.5 Nothing ran the test suite.** The queue's smoke only collects tests, and `ci.yml` ran on dispatch only, so units' "N passed" reports were the only evidence and main went red unseen. Fixed: #2588 and #2593 run the whole suite on every merge to main. Left: a PR check that runs the tests the diff touches, so a red test is refused before the queue rather than found after.

**9.6 CI output was invisible to units.** Hosted annotations show ten findings per step, so the debt ratchet's full list never reached the batch units: 88 findings sat behind "first ten". The ratchet writes its whole list to the job summary, which the API returns in full.

**9.7 Judging had two tiers.** Only the connector could record a head verdict, which judges every edit before a head in one call. The commander and its forks judged each edit alone, and 436 unjudged edits became load nobody could clear. Fixed: #2599 gives every commander and fork `judge head`.

**9.8 The chokehold graded a quote.** Units resumed when the answer quoted the indictment, whether or not anything cleared. Load divided debt by "working" forks that never record activity. Fixed: #2598 holds units until load and owed answers clear, and load is the debt itself.

**9.9 Commander CLI calls take 10 to 60 seconds.** `order say` 10 to 44 s, `order send` up to about 60 s, three `order done` 128 s (the commander's turns, 2026-10-21 20:30-20:48). #2579 removed one cause, the directive re-reads. The profile on the live bus names the next. Target: under 3 s per call.

**9.10 Three hand-written `gh api` subprocesses.** `head_review.py`, `todos.py:613`, and the ruleset reader in the phase evidence in flight. One GitHub reader on `RepositoryName` owns every call.

**9.11 Cross-repository pairs break each side's checks.** Core's checks import Toad's main and Toad's import Core's, so every rename (P434's `RouteChangedError`, the batch's `candidate_maintenance`) is red on both sides until both land. The stack lock in wave 8 owns this: both repositories' checks read one committed lock, and a pair bumps it in one change.

## Execution from 2026-10-22: every wave is a phase, or it never moves

Waves 4 to 8 were written on 2026-10-21 and declared no phase in `plan_phases.py`, so the commander's plan rules never named them and no unit was ordered. An inventory with no phase is a list nobody runs. Each item with a checkable end state is now a phase with evidence on main, so PLAN IDLE names it until it is ordered and PLAN STALLED names it until it lands:

| Phase | Item | Done when, on main | Owner |
|---|---|---|---|
| thread_name_owners | 4.1 | no `owner_thread` in src | one unit, after the bus batch (carry) |
| turn_owner_identity | 4.5 | no `owner_pid` in src | one unit |
| format_identity | 4.7 | no `format_fingerprint`, `_canonical_schema`, `convert_format` or `source_fingerprints` in src | one unit |
| directory_fsync | 5.1 | no `fsync_parent` flag in src | one unit with platform_facts |
| platform_facts | 5.2 | no `os.name` in the four named private-mode files | same unit |
| failure_reason | 6.2 | no `FailureReason` in src | one unit |
| hand_precedence | 6.1 | no integer `precedence`, `priority`, `classification_priority`, `proof_order` or `action_order` assignment in src | one unit, before failure_reason |
| dead_tools | 8 | the two dead tools absent | any unit, one PR |
| maintenance_gate | 9.1 | no hand `assert_open_unlocked()` in `agent_activity.py` or `wire_log.py` | the bus batch owners |
| transcript_hold | 9.3 | no `transcripts_held` or `HOLD_TRANSCRIPTS` in the installer | claude-ai |
| installer_file_identity | 9.2 | no string-typed `rewritten`, suffix `endswith`, or `name[:-4]` in the installer | claude-ai |
| context_request_key | 4.2 | no `thread_name` in `context_store.py` | one unit (carry) |
| turn_state | 4.4 | no `last_finished_turn_id` in src | one unit |
| wire_renames | 4.6 | no `WireName("admission…` in src | one unit, Toad in lockstep |
| owned_dispatch | 5.4 | no `TurnInboxReceiver`, no hand `separators=(",", ":")` | one unit |
| kind_families | 6.3 | no `TagAction`, `ExecutionOrigin`, `WhenBusy`, `ControlClassification`, `MembershipChange` or `ViewKind` enum classes | by file |
| rosters | 6.4 | internal registries and capability/name/path rosters derive from their owners; retain independent Pi producer/converter coverage | by family; shared phase proof has one owner |
| records | 7 | no `ConnectivityFacet`/`observe_connectivity`; no `nonlocal` in the five document updaters | one unit |

9.4 is Tristan's ruleset change. 9.5, 9.6, 9.9, 9.10 and 9.11 are claude-ai's and are measured by their own numbers in this section: a touched-tests PR check, the full ratchet list in the job summary, commander calls under 3 s, one GitHub reader, and one stack lock. The order across plans stays Tristan's: the virtual bus batch first, then multiple inheritance, the commander-native plan, then this plan. Phases with no stored data run in parallel with all of them on disjoint files.

## Done means

Each phase, its predecessors and the evidence on `origin/main` that proves it done are declared in `src/agent_comms/plan_phases.py`; the commander's plan-idle rule and order refusals read that family, and accepting a task never marks a phase done.

Each surface's guard is at zero on `main`, its deleted code is gone with no alias left behind, and the census at the new head shows the measure down by the surface's count. Report lines deleted first.

## Order of work

| Wave | Surfaces | Parallel | Stored data touched |
|---|---|---|---|
| 0 | 0.1 to 0.4 | one PR | none |
| 1 | 1.1, 1.2, 1.3 | three units | none |
| 2 | 2.1 to 2.5 | by file; one unit owns `commander/order.py` | only the staged install file, reset |
| 3 | external boundaries | as capacity allows | none |
