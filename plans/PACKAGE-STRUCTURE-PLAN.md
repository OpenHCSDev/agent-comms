# Package structure follows behavior ownership

> **Done.** All 5 phases of this plan hold their evidence on main (read by Core's PlanReading, 2026-11-06). Kept as the record of what was decided; it is not work.

This is the package-structure plan requested in `OWNER-ASKS-PLAN.md`, against
Core main `715c3c18e46c987949c28eab744451e03ad76f9e` (#3511). It plans the complete
flat production surface; it does not move modules or change stored data.
`PackageStructurePlan` and its phases in `src/agent_comms/plan_phases.py` put the
work on the existing commander board. The original owner ask is planning work;
these new phases track implementation separately.

## Evidence and the first decision

The original ask counted 373 flat modules. The current canonical NRA
`ModuleSyntaxIndex` census parses 591 production modules and 5,614 classes with
zero parse omissions, excluding the nested skills distribution. There are now
393 direct Python files in `src/agent_comms`, including its package initializer.
NRA's `SourceModuleImportGraph` records 4,633 top-level internal import edges.
Conditional imports, function-local imports, dynamic loading, entrypoint strings
and imports from sibling repositories require the consumer pass below; that
number is not a complete runtime dependency graph.

The retained complete NRA scan at `437f51f3b623a3c1eb46253c7170e494e1e2a01a`
covers 616 Core/dependency files and all 90 detectors. Reuse its source manifest,
class-family projections and findings, then reprove changed dependencies. Do not
rerun the whole scan after each file move. Current census and import evidence:
`/home/ts/.cache/agent-scratch/codex-greentext/package-import-evidence.json` and
`scoped-store-thread-ast-after.json`. Record the next implementation head and
its source evidence in the implementing PR so these observations never become
an import roster maintained by hand.

The highest fan-in owners are FieldCodec (352 direct importing modules),
DeclaredFamily (159), errors (128), Comms (108), coordination errors (102),
thread identity (77), process identity (74), Checkout (68) and TypedTable (66).
FieldCodec imports only DeclaredFamily, json_schema and sealed within Core.
DeclaredFamily has no top-level Core import. Start with this bounded declaration
cluster; do not start by splitting Thread or moving the runtime facade at random.

## Destination ownership

Keep existing coherent packages: `commander`, `installer`, `cli_support`,
`context_segments` and the hooks package. Keep `comms.py` as the actual composition
root and existing executable entry modules at the root while they own executable
behavior. The root initializer stays empty of API reexports. Every other flat
module joins the package of the behavior it implements, regardless of filename.

| Package | Existing owners and initial source closure | What belongs here |
|---|---|---|
| `nominal` | DeclaredFamily, FamilyMeta, FieldCodec; `declared_family.py`, `field_codec.py`, `json_schema.py`, `sealed.py` | Declaration, discovery and boundary conversion mechanisms. No bus, runtime or UI selection. Preserve external metaclass/MroDispatch contracts. |
| `storage` | StoreLock/tiers, LockedStore, PrivatePath, maintenance barrier, TypedTable | File/SQL capture and bounded publication, path identities and declared storage policy. Registry-specific mutations remain in registry. |
| `coordination` | Coordination, CoordinationStore, coordinator vocabulary; existing `coordination_tables` moves to `coordination/tables` | Execution records, attempts, assignments, leases and their SQL transactions. Extend these owners; no second coordinator or family catalog. |
| `registry` | Thread, RegistryDocument/Snapshot, RegistryStore, Registration, thread identity/configuration, ThreadManagement; a new native thread's default model: `native_settings.py` | Participant declarations, current generation, names and aliases, project and saved-session registration. Process launch stays with execution. |
| `execution` | OwnerLifecycle, SessionLifecycle, InputDrain, TurnRunner, OwnedTurn, owner restart and shared worker; the selected-turn family: `selected_actions` moves to `execution/selected/actions`, `selected_claim` moves to `execution/selected/claim`, `selected_participant` moves to `execution/selected/participant`, `selected_request` moves to `execution/selected/request`, `selected_result` moves to `execution/selected/result`, `selected_session` moves to `execution/selected/session`, `selected_source_batch` moves to `execution/selected/source_batch`, `selected_tool_broker` moves to `execution/selected/tool_broker`, `selected_triage` moves to `execution/selected/triage`, `selected_turn` moves to `execution/selected/turn`, `selected_write_authority.py` merges into `selected_actions.py`, `selected_write_plan` moves to `execution/selected/write_plan`; the native Pi child, its launch, session files and input evidence: `native_arguments.py`, `native_attestation.py`, `native_child.py`, `native_claim_epoch.py`, `native_entries.py`, `native_evidence_read.py`, `native_failures.py`, `native_file_artifact.py`, `native_fork.py`, `native_input_owner.py`, `native_input_proof.py`, `native_input_record.py`, `native_integer.py`, `native_output.py`, `native_pi.py`, `native_prompt_binding.py`, `native_prompt_send.py`, `native_runtime_input.py`, `native_session_files.py`, `native_session_prepare.py`, `native_session_reopen.py`, `native_startup.py`, `native_transcript.py`, `native_turn_context.py` | Hosted owner/session lifetime, native turns, input draining and retirement. Retain independent concurrent turns in one host. |
| `messaging` | MessageBus/reader/publisher, MessageReference, target families, channel catalog, read basis and delivery cursors; `messages.py`, `read_basis.py`, `message_reference.py`, `channel_targets.py`, `channels.py` | Committed conversation history, routing and revision-derived display selections. No duplicate widget state or coordination authority. |
| `planning` | Goals, GoalWait/GoalWaits, Todo/order records, Plan/PlanPhase and evidence | Persistent goals, request/order transitions and plan evidence. Commander rendering remains in the existing commander package. |
| `transport` | ACP/extension/request families, controller and runtime request/server owners; native tool declarations and per-call tool routing: `native_tools.py`, `native_tool_transport.py`, `native_tool_call.py` | External framing and request routing. Native process policy stays in execution; original input authority stays with its existing owner. |
| `projects` | Repo, Checkout, repository and worktree operations, project configuration | Filesystem/repository identity and checked project operations. No generic utilities package. |
| `installer` | Runtime build, install and carry; the pinned native Pi package and its build inputs: `native_package.py`, `native_build_inputs.py` | Building, pinning and installing the runtime and its native package. A running session reads the installed package; it never builds one. |

These are semantic destinations, not prefix-matching rules or a second runtime
registry. The implementation owner classifies every remaining module from its
actual declarations and consumers. A module shared by several consumers belongs
to its determining behavior owner. Break a cycle by factoring shared behavior
into that existing lower owner; do not replace it with local imports, reexports,
a service locator or a newly named helper package.

## Ordered migration

The existing shared fleet must first have its measured memory budget and the
scoped-store migration completed. `PackageDeclarationsPhase` depends on those
two original phases. This prevents concurrent import churn through unfinished
runtime and store changes. No new units start merely because this plan exists.
One integration owner carries each dependency-closed move through both consumers
and installation; keep retained P861 edits outside these moves until integrated.

1. **Package declarations.** Move the four-module nominal cluster. Follow every
   import from production, tests, tools, Toad, Textual, ui-theme and the installed
   skill packages. Keep one class object per declaration; delete the four old
   modules in the same change. Preserve family registration, MRO, codec tags,
   discovery order and abstract-member exclusion. Do not rename durable tags.
2. **Package storage.** Move the store/path/maintenance family and TypedTable
   only after enumerating its complete dependency closure. Retain class-declared
   guard rank and async capture/publication policy. Do not create package-level
   lock wrappers or hold a writer across decode to make imports easier.
3. **Package coordination.** Move the coordinator and its existing table package
   as one schema/discovery closure. Migrate CoordinatorVocabulary consumers,
   runtime-wire imports, startup schema installation and private-store readers.
   Discovery imports are live even when no Python name is read from them: an
   earlier retained experiment reduced native tools from 64 to 4 by deleting
   one. Table names, indexes and schema ownership must not change during the move.
4. **Package execution.** Move shared-host, lifecycle, drain and turn families
   together with their runtime consumers. Preserve explicit per-session state,
   owner identity/generation, startup grants, task-local controller and backend,
   signal ownership and joined retirement. No fallback per-unit worker system.
5. **Close consumers.** Move registry, messaging, planning, transport and project
   families in dependency-closed batches. Inventory every remaining flat module
   against the destination table; keep only actual root composition/entry code.
   Update all phase evidence paths when their owners move, including the original
   package-plan evidence, rather than leaving a permanently false completed ask.
   Finish all consumers in Core, Toad, Textual, tools, fixtures and generated
   installer configuration. Delete every old import path in its move batch.

The five phase declarations require the old files to be absent at each
checkpoint. Their deletion predicates track removal of the flat paths; they do
not prove destination ownership, behavioral parity or complete consumer closure.
The destination table and required source pass specify those obligations. A
phase is accepted only with that closure and working application evidence.
The board must not import execution owners merely to spell their names: doing
so creates the PlanPhase → InputDrain → Todo → PlanPhase initialization cycle.

## Required source pass for each move

Use NRA's canonical module identities, `ModuleSyntaxIndex`, class-family
projection and `SourceModuleImportGraph`. Enumerate original ClassDefs and
conditional/nested classes before selecting moved declarations. Retain unresolved
bindings as OPEN. Trace bases, metaclass, decorators, registration effects,
annotation evaluation, imports, writes and every consumer across the paired
repositories. Include string module references in launch commands, entrypoints,
FieldCodec/family discovery, installer probes, skill loading and test fixtures.
A top-level graph edge alone is insufficient evidence to delete an import.

Use the existing dependency-closed module/symbol movement operations, revision
checks and targeted reproof when they support the closure. An unsupported move
gets an explicit authored patch and a named proof gap; do not fabricate a native
execution proof from a serialized report. Review one combined diff. An import
cycle introduced by the proposed destination rejects that move until its real
shared behavior has an owner. Keep correct external wire/CLI contracts and
original saved data; internal import compatibility is explicitly forbidden.

At each batch, publish the source revision, destination declarations, all migrated
consumer roots, parse omissions, unresolved bindings, deleted old paths and
new-case result in the PR. Do not add a second module inventory service, writable
migration manifest, source-generated compatibility files or import fallback.

## Acceptance and maintenance

- Source closure: all intended consumers import the defining module, old modules
  are deleted, no alias/reexport files remain, no extra family registry or codec
  exists, and the wheel contains the new packages. Check both import orders for
  a moved family with circular consumers and preserve class identity.
- Registration closure: the native tool set, coordinator vocabularies and schema
  derive from their declarations before and after the move. Compare actual members,
  not just counts. Exercise a genuinely new member through its existing discovery
  owner to check that adding it requires no external roster edit.
- Runtime closure: use the existing three-owner shared-host journey for concurrent
  replies, attachment, rename, stop-one, restart-one and joined shutdown. Measure
  against the existing memory budget; moving modules must not add interpreters.
- User closure: install current paired mains and exercise the continuous native
  Toad journey: saved-history startup, unopened thread, fork with immediate open,
  first input and reply, fresh DM while its tab is open, status/history updates,
  tab return and reopening. Check retained commander disclosures across heartbeat.
- Data closure: original sessions, source history, auth and failed/UNKNOWN inputs
  stay intact. Migration never authorizes retrying an input. No stored format
  change is part of package movement.

Run proportionate combined checks after coherent source changes, then the affected
installed path. Hosted structural gates remain required. Do not run a local
ratchet/full suite per file or rebuild unchanged native dependencies per move.
Remove owned disposable artifacts after acceptance and active-borrower checks;
retain concise source/results in the PR and preserve input evidence.

> 393 files in one directory
> folders are not the missing authority
> move the owners and all their consumers, delete the old addresses

## Tool declarations live in transport; implementations compose by multiple inheritance

Every `ToolRequest` member fuses two providers: its **wire declaration** (fields,
`declared_name`, description, result) and its **implementation** (`apply()` and every
module `apply()` imports). The client needs the first; the server needs both. That is a
crossing (paper1 §1, Fig. 2), so one parent per class leaves the client importing the
server: `import toad.app` (3.18 s) loads `agent_comms.todos`, `cli_commands`, both CLI
bridge wire protocols and `team_hud`, about 370 ms of import work, because Toad
constructs `TranscriptPageRead`, `TeammateRead`, `InputDeliveryRead`, `OwnerGoalRead` and
the `agent_comms.tools` requests its sidebar sends. The edges, measured on main
`e5e90ee1` with `python -X importtime -c "import toad.app"`:

- `toad.acp.status -> agent_event_updates -> transcript_updates -> order_tools`
  (`TranscriptPageRead`), then `order_tools -> team_hud -> cli_bridges.codex_*`.
- `transcripts -> turn_digests -> held_orders -> todos` (`HeldOrders`, `LiveHold`).
- `toad.widgets.comms_sidebar -> agent_comms.tools -> cli_verbs.{archive,delete_view,fork}
  -> cli_commands`, and `tools -> team.standing -> cli_bridges.claude_*`.

Multiple parents take that outside pair to zero (Proposition 4.2):

- **Declaration** (`transport/tools/…`): the `ToolRequest` member with its fields, name,
  description and its result bound in its class header. It imports nominal, identity and
  message types only, never planning, CLI or bridge modules.
- **Implementation**: a mixin carrying `apply()`, in the package of the behaviour it
  implements (registry, execution, planning, messaging).
- **Served tool**: `class ServedX(XImplementation, X)`. It inherits the declaration's
  fields and wire name; nothing is restated. The connector decodes a wire name into its
  served member, which registers itself at `__init_subclass__` from its MRO. No roster.

A client constructs and posts the declaration; only the server imports the served
module. The result is part of the wire contract, so the declaration binds it in its header
(`ToolRequest[Result]`) and the client decodes replies from it; the served `apply()` returns
that type and mypy checks the override. (While the classes were fused, #4194 read the result
from `apply()`'s annotation; the split moves the declaration to the header.) The caller
family (`tool_caller`) is the server's model of who calls, so `Caller` is a parameter of the
serving side (`ServedRequest[Caller, Result]`), not of the declaration.

### Step 0: the base

`agent_comms.tool_request` imports `agent_comms.comms`, the composition root, and
`tool_caller` (which imports `cli_bridges.session_watch`), only to annotate the serving
methods: `import agent_comms.tool_request` alone is 1.5 s and loads `todos`. Its five
direct subclasses (`ObservedToolRequest`, `HumanToolRequest`, `NativeToolRequest`,
`PublishingToolRequest`, `CoreRead`) are where the serving mixin enters. Declaration
members: `schema`, `called`, `result_type`, `context_bindings`, `posted`, `dropped`, the
field directives (`tool_field`, `ContextBinding`, `ListedNames`, `OmittedNames`) and the
`ToolCall` wire record. Serving members: `invoke`, `decoded`, `replied`, `reply`, `board`,
`apply`, `apply_without_thread`, `context_types`, `bound`, `served_by`,
`require_read_only` and `ConnectorService`. Done when `import agent_comms.transport.request`
loads no `agent_comms.comms`. Shared behaviour across implementations stays on its existing parents
(`PublishingTool`, `ReadTool`, `HumanToolRequest`); a declaration family's shared fields
stay on the declaration parents.

### Phases

1. **UI reads.** The `UiRead` members Toad and `ui_model` construct
   (`TranscriptPageRead`, `TeammateRead`, `InputDeliveryRead`, `OwnerGoalRead`) and the
   served mechanism itself. Done when Toad's acp/status side (`toad.core.events`,
   `toad.acp.*`, `toad.session_scope`, `toad.screens.{team,historical_sessions}`) reaches
   `order_tools` only through `agent_comms.tools`. (The earlier criterion, `toad.app` loads no
   `order_tools`, cannot hold until phase 2: `comms_sidebar` and `session_agent` import
   `agent_comms.tools`, which imports `order_tools`. Measured in #4220: `toad.core.events`
   3,027 → 2,406 ms, `toad.app` unchanged at about 3.16 s.)
2. **The sidebar's requests.** Every request `agent_comms.tools` exposes to Toad. Done when
   `import toad.app` loads no `order_tools`, `cli_commands`, `cli_verbs` or `cli_bridges`
   module. Scope, read from Toad main's imports (AST, not grep): 36 names in 16 files.
   `comms_root.py` (Registry, HistoricalThreads, MarkThreadViewRead, ServedRoot,
   SessionThread, UserSend), `referenced_core.py` (Registry, RegistrySlice, ReferenceNames,
   WireRevision), `core/context_reads.py` (ContextInspection, ContextInspectionRead,
   ImportedSource, OwnerRequest, SavedSegment, SavedSource), `acp/session_agent.py`
   (CaptureHumanInput, MoveThreadProject, OwnerRequest, RenameManagedThread),
   `conversation_kind.py` (Channel, ConversationPage, ThreadConfiguration),
   `wire_history.py` (ConversationRevision, LatestSequence, MarkPageRead),
   `widgets/comms_transfer.py` (ExportWire, ImportThread), `thread_actions.py` and
   `widgets/comms_command_dialog.py` (PrepareTargetAction, TargetEdit),
   `widgets/thread_comms_source.py` (SetRelationshipOrder, ThreadRelationships),
   `widgets/comms_sidebar.py` (SortChannel), `widgets/session_sort.py` (SortChannels),
   `submission_queue.py` (QueueAction), `target_commands.py` (TargetActions),
   `session_navigation.py` (RetiredViews), `acp/transcript_reader.py`
   (ThreadPresentation). Each becomes a declaration under `transport/tools/` with its
   result in its header and a served member in `tools.py`, by the same codemod as phase 1.
3. **Every remaining tool**, by codemod from one retained scan. Done when no declaration
   module imports an implementation module, and `tools.py` and `order_tools.py` hold no
   `apply()`. `ServedRequest.result_type`'s fallback to `apply`'s annotation is deleted: every
   result is bound in a declaration header.
4. **Result records move with their declarations.** A declaration binds its result in its
   header, so the result record is part of the wire contract and must be as light as the
   declaration. Today `TranscriptSnapshotUpdate` and `GoalSnapshotReply` live in
   `acp_extension` (2,559 ms alone, beside `order_tools`' 2,704 ms) and `TeammateDetail` in
   `team_hud` (514 ms, reached through `transport.tools.reads`). Done when no module under
   `transport/` imports `acp_extension`, `team_hud`, `todos` or `transcripts`.

A served class is its declaration's value: it compares and hashes as the declaration, because
a process that has served once decodes `ServedX` while it still constructs `X`, and a reply
must find the request that asked (#4220). `ToolDeclaration.serving(request)` is the one way a
process turns a declaration it holds into the member that serves it.

Each phase ships its import measurement before and after, and the remote connector run
(a tool decoded by wire name reaches its served `apply`) as proof.
