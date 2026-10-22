# The harness: make commanding playable again

Decision (Tristan, 2026-10-21 22:45): the commander fixes the harness it commands with. It works with full authority on this plan only: it makes each change, inspects it with full attention, runs only the tests the change touches, and pushes straight to `main`. Audited against `origin/main` at `39fdfe810`.

## Why

On 2026-10-21 the plans Tristan asked for at 18:30 the day before had not landed 28 hours later. Every error pulled the fleet off them, and the errors came from the harness itself:

- Install 52 sat for twenty minutes holding the wire lock and waiting on the registry lock (`maintenance_barrier.py:380`, `store_files.py:157`), sleeping in short naps and never naming the holder. The live coordination store was found malformed in the same minute the install was stopped.
- "May I write during an install" had five answers: the maintenance barrier, a transcript lock, a connector stop, a bridge watch stop, and the commander's shell wrapper around `agent-comms install`. Each install failed on a different missing one.
- One commander verb took one to five minutes under load. About 4.8 seconds of each call was import alone (2,093 dataclass builds, 433 pydantic models), and the answer gate then scanned the bus.
- Failures were silent. The bridge watch crashed when the socket moved during the install (`cli_bridges/monitor.py:260`, FileNotFoundError). The session relay would not restart ("Session bridge startup not observed ... No launch retry"). Commander writes hung until a timeout killed them, with no output. A say that drew a jargon flag appeared to record none of its answers; on 2026-10-22 the message itself (`#comms` seq 18462, 34 answers) showed they are recorded, and the report came from a heartbeat replayed from before the post.
- About 120 bus events in 30 minutes reached the commander's main loop, including six-part heartbeats listing twenty vibe-check ids.

A silent or slow tool teaches the commander to work around it, and the workarounds are new copies of facts. The fix is at each owner, once.

## Steps, in order

### 1. The live stores stay whole, and lock waits name their holder

- `CoordinationStore.observed()` (`coordination_database.py:220`) and `is_database()` (`:118`) open a plain handle on the live database inside processes that hold SQLite locks on it; closing that handle drops the process's locks. This is the likely cause of the 2026-10-21 corruption (fork AL's forensics). The required answer is what sits at the path (missing, not a regular file, not our database, our store); the store's own read-only connection answers it, so the header read is deleted. Callers: `transcripts.py:397`, `coordination_tables/refusals.py:71`, `notification_assignment.py:350`, `installer/backup.py:271`.
- Installer backup and restore (`installer/backup.py:271-278`, `:360-366`) take the store's own lock instead of relying on writers having been stopped.
- The bus wire lock, its hand-spelled paths and the store lock wait (`store_files.py:157`, which must fail within seconds naming the holder) belong to the virtual bus batch (#2549), which rewrites them; this plan does not touch them.

**Done when** no code outside a store's owner opens its live file, and a lock held during an install is named in the install log within seconds.

### 1c. Installs never let a second runtime change the live stores

Found 2026-10-22: a store writer reconciles any schema difference on open (`coordination_database.py:262`, `typed_table.py:1812`, `goal_history.py:156`, `cli_bridges/shared.py:170`), so any newer-code process that opens the live bus migrates it, outside maintenance and before the backup. Install 53 left the live coordination store on the new schema and aborted; the old runtime then refused the store and every unit failed to restart.

- `reconcile_schema` takes the writer, and inside its existing schema-differs branch asks `writer.require_schema_change()`: allowed only while this process holds the bus's maintenance hold (`MaintenanceStatus.require_held_by(holder)`: the open member refuses, a held member compares nonces). One rule for every StoreWriter; fresh empty stores still create their schema.
- The installer reconciles each declared store under the hold, after the backup, with the staged runtime, so owners start against stores that already match.
- Step dry runs and `before_stop` run on the practice copy and under the hold, never on the live root before it.
- `ingress_async` holding a shared lock across its caller's awaits (the fleet's shared locks starved installs 52 and 53 of the exclusive lock) is #2549's: that batch rewrites `ingress_async` onto the registry lock and still yields inside it (PR comment, 2026-10-22).

**Done when** a newer runtime opening the live bus outside an install fails loudly naming the store and the hold it lacks, and an install with running units completes without anyone stopping them by hand.

### 1d. Main's own tests pass

On 2026-10-22 the full CI workflow on main showed 197 failures, 127 of them in `tests/guards/test_debt_ratchet.py`, and the merge queue's install rehearsal would have failed every PR queued after #2595. A red on main makes every later run ask whether a red is real (rules/tests-and-commands.md). Every red is fixed at its owner or handed to a named open PR, one cause per change.

- Declared evidence paths: a path a plan phase names that main never held reads as absent and passes (`phase_evidence.py` `present_modules`); a guard proves every declared path exists in git history.

**Done when** the CI workflow passes on main, and a red that lands is owned the tick it appears.

### Installer completion in progress

The installer is the current priority before resuming the other plans. Keep defects
found while tracing it in this change, including these unfinished repairs:

- [x] Read the old stores with the outgoing runtime before migration, and the migrated
  stores with the incoming runtime afterward, in rehearsal and live installation.
- [x] Let the maintenance hold own its permission's lifetime on success and recovery.
- [x] Carry declared operations and their answer types through the existing codec;
  keep migration results separate from diagnostic output.
- [x] Replace inline interpreter probes throughout the installer, including package
  resolution. Ask the selected runtime without accidentally importing the checkout.
- [ ] Repair affected test setup, then exercise rehearsal, failure recovery and the
  installed entry path. Report source checks and actual installation separately.
- [ ] Finish shared SQLite ownership: file copies, digests, connections and snapshots
  belong to `SQLiteFile`; coordinator schema/session behavior stays in its subclass.
  Migrate the generic consumers and delete the subclass dependencies.
- [ ] Repair the paired Toad test family's stale agent ownership and launch setup,
  then qualify a saved fork through immediate opening, a fresh DM, its first reply
  and tab return. Fix demonstrated application defects uncovered on this path.

#### Implemented ownership and migration order

`before_stop` had no implementations in main or the pending virtual-bus branch.
Delete that phase rather than designing recovery around dead work. Live order is:
hold maintenance; stop outgoing owners while their store format still exists; capture
the original files and outgoing read; finish the backup; migrate and reconcile; read
with the incoming runtime; compare against rehearsal; publish; release maintenance;
restart. Taking a backup before owners stop would allow their final writes to fall
outside recovery.

`ChangedInstall` now owns rehearsal and live installation. `PreparedMigration` owns
the original snapshot, outgoing read and common migration/check sequence;
`BackedUpMigration` adds recovery from `StoreBackup`. `MaintenanceState.holding`
owns permission lifetime, including restoration after nested holds fail. Delete the
detached `rehearse`, `live`, `before_stop`, `carry_all` and `carry_and_read` paths.

`CarryOperation` owns invocation arguments and behavior. `InterpreterOperation`
derives answer encoding from the operation's return annotation through `FieldCodec`.
Workers write answers separately from diagnostics, replacing dynamic method lookup,
boolean argument bags and last-stdout-line parsing (IMPL-7). Runtime probes use that
same answer contract under the selected interpreter; `InstallCommands` owns process
execution and timing. Launch, native tests and package resolution use that owner.

Source inventory uses NRA's parser, `ModuleSyntaxIndex` and `ClassFamilyIndexBuilder`
over installer modules, maintenance and cutover declarations. Dynamic cutover imports
remain resolved by `CarryChain` at runtime; AST evidence does not prove their behavior.
The current pass parsed all 15 modules and projected all 113 original class declarations.
The existing CI ratchet misses the old computed-method dispatch, stdout protocol and
manual permission lifetime: its literal-name and multi-arm measures do not establish
these ownership relationships.

Validation: 57 focused checks passed across carry execution, maintenance, package
resolution, publication and install announcements. A real worker that writes then
fails is restored from its backup. New probes ran under the outgoing installed
interpreter and matched all 82 recorded packages. Five actual CLI rehearsal/native
runtime checks passed. A complete CLI rollback test also passed: a real carry worker
succeeds in rehearsal, writes then fails on the live private bus, and the installer
restores the resource, checkout, runtime pointer, launcher, maintenance state and
retained thread. Those results cover `f47cda95e`.

SQLite connection, snapshot and copy behavior now belongs to `SQLiteFile`;
coordinator, journal, installer and typed-store consumers use that owner.
`DurableStore` composes installation reconciliation; `DerivedStore` owns runtime
rebuilding and no longer enters the installer's carry set. This fixes disposable
indexes demanding maintenance permission to rebuild. The final permission batch
passed 47 checks, including planned/reconcile CLI, real session bridge, goal
history, snapshots and required maintenance refusals. Earlier connection/copy
batches passed 177 and 120 checks. NRA parsed all 461 production modules without
omissions; dynamic resolution remains a semantic review responsibility. The
complete CLI run against this committed batch is next.

The saved-fork application journey passed against built Core `f47cda95e` and Toad
`ca980312c`: real native parent answer and saved history, fork, immediate opening,
fresh DM, first native reply, tab return and unchanged parent bytes (33.29 seconds).
The loopback provider supplies replies; UI, ACP, native process and durable state
are real. The paired Toad journey exposed stale fixture contracts:
`Conversation.agent` was deleted for
`agent_binding`, Core's model/thinking fields are typed, and its ACP declaration
must opt into explicit root arguments. The native provider fixture also retained a
deleted response-template protocol; current selected work returns ordinary answer
text. These are being repaired in their existing owners and consumers, not patched
with production aliases. A closed delivery observation additionally reads an agent
before it has ever scheduled a read; the Toad integration owner is fixing that
production lifetime defect. The same displayed journey exposed raw delivery-state
dataclass text in the status line; `ThreadStatusModel` now uses the existing
`MessageNotification.label` owner (38 focused status checks passed).
No new runtime has been published to the live installation. The final changed
packages still require installer rehearsal/recovery and installed journey checks.

### 2. Commander verbs take a fixed, small time

- Profile one verb end to end on the live bus with nothing else running.
- Cut import cost (load only what the verb uses) and make the answer gate read only the rows it needs, never the whole bus.

**Done when** `order say` and `order board` each answer in under one second on the live bus.

### 2b. The growth alarm counts code, not imports

The growth alarm is commander-native phase 7's (`plans/commander-native-plan.md`, Growth alarm); this step only fixes its measure. Today three places count raw lines (`git diff --stat`, the alarm, PR bodies), so a factoring that turns 22 declarations into one reads as +18 when twelve of those lines are top-of-file imports. One AST-based measure drops the leading module-level imports, blank lines and comments (imports below the top still count), the alarm and PR bodies read it, and each report shows it beside the ratchet's fact measures.

**Done when** the alarm is the only place that counts net source lines and it ignores top-of-file imports.

### 3. No silent failures

- The bridge watch's running loop already survives a missing socket while its owner lives (`cli_bridges/monitor.py:392-411`). A watch started during an install still gives up after a hand-picked 30 seconds (`InspectSession.awaited`, `monitor.py:305`); it waits while the bridge process it is attaching to is alive (that PID survives the bridge's exec in place) and names the wait.
- The session relay restarts, or says exactly why it cannot.
- The owed-answer set clears when an answer is recorded: shown on a live heartbeat after an answer posted to `#comms`. Until then it is not known whether it does.
- `--answers` refuses a malformed id (for example a comma-joined list) at the command line.
- A commander write that waits on a lock prints what it waits on.

**Done when** each case above, reproduced once on a practice copy, ends in a named answer or a recorded result.

### 4. Plans that read as one

- Delete the dead files in `plans/`; keep the three live plans and this one.
- Delete `REMAINING-SLOP-PLAN.md` or `remaining-slop-plan.md`, whichever is superseded.
- The multiple inheritance plan keeps one version: its corrections replace the original body instead of sitting above it.

**Done when** `plans/` holds only plans someone is executing, and each states its target once.

## Owned elsewhere, not restated here

- Pings instead of a firehose: commander-native phase 6, the commander page (HUD).
- The bus lock and every `wire` path: the virtual bus batch, #2549.

## Rules for this plan

- Direct pushes to `main` for this plan only. Every other plan still lands through the merge queue.
- Run only the tests the change touches, locally, in the background under a timeout.
- Each change deletes what it replaces in the same commit.
- Never touch a file another plan deletes (2026-10-21: the reverted 047bccc65 added 16 conflicts to #2549).
- Say what is slow or broken the moment it shows, out loud, instead of working around it.
