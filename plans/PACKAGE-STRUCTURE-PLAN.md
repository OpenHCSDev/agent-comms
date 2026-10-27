# Package structure follows behavior ownership

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
| `registry` | Thread, RegistryDocument/Snapshot, RegistryStore, Registration, thread identity/configuration, ThreadManagement | Participant declarations, current generation, names and aliases, project and saved-session registration. Process launch stays with execution. |
| `execution` | OwnerLifecycle, SessionLifecycle, InputDrain, TurnRunner, OwnedTurn, owner restart and shared worker | Hosted owner/session lifetime, native turns, input draining and retirement. Retain independent concurrent turns in one host. |
| `messaging` | MessageBus/reader/publisher, MessageReference, target families, channel catalog, read basis and delivery cursors | Committed conversation history, routing and revision-derived display selections. No duplicate widget state or coordination authority. |
| `planning` | Goals, GoalWait/GoalWaits, Todo/order records, Plan/PlanPhase and evidence | Persistent goals, request/order transitions and plan evidence. Commander rendering remains in the existing commander package. |
| `transport` | ACP/extension/request families, controller and runtime request/server owners | External framing and request routing. Native process policy stays in execution; original input authority stays with its existing owner. |
| `projects` | Repo, Checkout, repository and worktree operations, project configuration | Filesystem/repository identity and checked project operations. No generic utilities package. |

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
