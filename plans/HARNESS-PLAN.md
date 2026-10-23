# The harness: make commanding playable again

Decision (Tristan, 2026-10-21 22:45): the commander fixes the harness it commands with. It works with full authority on this plan only: it makes each change, inspects it with full attention, runs only the tests the change touches, and pushes straight to `main`. Audited against `origin/main` at `39fdfe810`.

## Expanded installer integration, 2026-10-22

Tristan requested the next paired installation include the useful remaining open
work. Core's virtual-bus checkpoint passed the full configured-root rehearsal:
19,182 records, 194 threads, preserved read ledger, 208.38 seconds. That is a
rehearsal result, not an installed checkpoint.

Root owns porting the remaining reporting and bridge-lifetime work from #2556
and #2527 into the current `ChangedInstall` and `PreparedMigration` family.
The old branches' detached migration paths stay deleted. Reports before carry
must execute in the outgoing runtime; reports after accepted carry use the
incoming runtime. Rehearsals publish nothing. The report lifetime includes
target-tree cleanup, so cleanup failure cannot leave a successful terminal post.
Bridge outcomes belong to the actual captured bridge and survive failed exits.

The reused finished checkout is `/home/ts/wt/harness-plan`, branch
`integration/installer-report-lifetime`. Before-source evidence is
`/home/ts/.cache/agent-scratch/root-installer-report-before.json`: NRA parsed
953 production/test/tool modules and retained 5,341 original class declarations;
5,151 project into the conservative family index. The remaining 190 original
declarations stay visible rather than being counted as resolved. No parse
omissions; source evidence does not prove behavior. Implementation and affected
CLI/application qualification remain open.

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

- Repaired: `CoordinationStore.observed()` and backup file classification now use `SQLiteFile`'s nonwaiting read-only snapshot. Plain header handles could cancel other connections' process locks; that mechanism was a suspected cause of the 2026-10-21 corruption, not a proven reconstruction of that event. The raw header reads and invented WAL-mode rejection are deleted. Transcript, refusal-history, notification-assignment and installer consumers share the repair.
- Installer backup and restore (`installer/backup.py:271-278`, `:360-366`) take the store's own lock instead of relying on writers having been stopped.
- The bus wire lock, its hand-spelled paths and the store lock wait (`store_files.py:157`, which must fail within seconds naming the holder) belong to the virtual bus batch (#2549), which rewrites them; this plan does not touch them.

**Done when** no code outside a store's owner opens its live file, and a lock held during an install is named in the install log within seconds.

### 1c. Installs never let a second runtime change the live stores

Found 2026-10-22: a store writer reconciles any schema difference on open (`coordination_database.py:262`, `typed_table.py:1812`, `goal_history.py:156`, `cli_bridges/shared.py:170`), so any newer-code process that opens the live bus migrates it, outside maintenance and before the backup. Install 53 left the live coordination store on the new schema and aborted; the old runtime then refused the store and every unit failed to restart.

- Durable schema changes require this process's maintenance hold through `ReconciledFile.schema_change` and `HeldBusFile`. Disposable, schema-named indexes rebuild through their runtime owner and stay outside installer reconciliation. Fresh empty stores still create their schema.
- The installer reconciles each declared store under the hold, after the backup, with the staged runtime, so owners start against stores that already match.
- Migration practice runs on the copy under its maintenance hold. The unused `before_stop` phase is deleted.
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
- [x] Repair affected test setup, then exercise rehearsal, failure recovery and the
  installed entry path. Report source checks and actual installation separately.
- [x] Finish shared SQLite ownership: file copies, digests, connections and snapshots
  belong to `SQLiteFile`; coordinator schema/session behavior stays in its subclass.
  Migrate the generic consumers and delete the subclass dependencies.
- [x] Remove raw database-header handles that can release the process's SQLite
  locks. Observation and backup
  classification use nonwaiting SQLite snapshots; the three observation consumers
  require their declared tables, not a particular journal mode. Delete the invented
  WAL refusal; let SQLite maintain its own companions without changing rows/schema.
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
complete CLI batch against `35c192054` passed all three cases in 178.78 seconds:
unchanged-bus rehearsal, drifted-store carry rehearsal, and a live migration that
writes then fails with data, checkout, launcher, runtime and maintenance restored.
That checkpoint is pushed to main. The subsequent raw-header repair passed 61
focused checks, 11 consumer checks and a final 15-case batch covering backup,
transcript contention and CLI/native held-send entrypoints. An independent process
remains unable to acquire a writer lock after coordinator observation or backup
digest, then acquires it after the original writer rolls back. WAL refusal-history
reads expose committed rows only, reject writes and preserve the writer's pending
transaction. The repair is pushed as `8ebcc4030`; all three complete installer
cases passed again in 183.45 seconds. Final paired installed-package qualification
follows the Toad repairs.

The saved-fork application journey passed against built Core `35c192054` and Toad
`ca980312c`: real native parent answer and saved history, fork, immediate opening,
fresh DM, first native reply, tab return and unchanged parent bytes (33.04 seconds).
The loopback provider supplies replies; UI, ACP, native process and durable state
are real. The paired Toad journey exposed stale fixture contracts:
`Conversation.agent` was deleted for
`agent_binding`, Core's model/thinking fields are typed, and its ACP declaration
must opt into explicit root arguments. The native provider fixture also retained a
deleted response-template protocol; current selected work returns ordinary answer
text. The repaired fixtures use the existing owners and consumers, without
production aliases. A closed delivery observation additionally read an agent
before it had ever scheduled a read; Toad now initializes and retires that binding
through its declared unbound state. Toad's follow-through also repairs transport failures
that lost their input disposition, recovery that replaced the retained editor and
undo history, and successful-send cleanup that erased a different unsent draft.
The permission journey exposed the pinned diff widget's obsolete render callback;
`textual-diff-view` now consumes Textual's supplied paint state (`95db444b`, pushed),
and its development lock uses the same Textual fork. NRA parsed the dependency's
two source modules and Textual's 255 source modules; the only dependency
`render_line(self, y)` override now implements `Widget.render_line(self, y, paint)`
and reads the supplied paint instead of querying widget state again. Its 14
existing tests and Toad's actual scripted ACP/file/permission journey passed.
Toad's dependency pin and recovery repairs are pushed in `c88eecf8d`; `5e02805dc`
also retains acquired transcript history through child teardown and applies link
metadata when displayed text is unchanged. The actual goal-link click, peer
opening, tab return and retained draft passed, together with the other five goal
controls and two unmount/application-close cases. The Toad binding fork retains
the unfinished historical fixture migrations independently of this batch.

The paired batch is installed: Core `c94bb280f`, Toad `5e02805dc`, Textual
`cdddff395` and diff renderer `95db444b`. The real CLI returned `installed`, read
194 threads before/after, required no durable rewrites or backup, and stopped or
started no agent owners. Connector round-trip and session-bridge runtime switch
completed. Its result is
`/home/ts/..agent-comms.installer-e66f6e3390f4412daebceb3c180aba48/result.json`.
The installer resolves sibling mains and records its runtime inputs;
`stack/uv.lock` is generated and untracked, not a separate source commit.
The same displayed journey exposed raw delivery-state
dataclass text in the status line; `ThreadStatusModel` now uses the existing
`MessageNotification.label` owner (38 focused status checks passed).
The installed saved-fork/fresh-DM/reply/tab-return journey passed. The additional
native client-request journey failed, but the old test harness labelled any
failure under its defect catalogue as expected and deleted the diagnostics.
Its recorded SelectCurrent defect therefore does not identify this failure.
The Toad fork has removed that outcome substitution in both app and ordinary
tests: the catalogue supplies context, and actual failed app attempts retain
their output and private roots. That repair is pushed in Toad `291fd4780`; its
actual-harness regression passed. A fresh installed client-request attempt timed
out after root shortened the harness's default 165-second budget to 90 seconds.
Its retained evidence is `/home/ts/.cache/agent-scratch/toad-test-7f90sh_5`:
the first permission/file edit completed, then replacement session startup had
roughly six seconds left before termination. The Toad fork's source trace found
no permission/startup dependency cycle. A fresh isolated attempt with the default
budget finished in 25.59 seconds and failed after replacement session creation:
`DB().session_get(saved)` returned `None` at the native test's title assertion.
Its full child output is retained in
`/home/ts/.cache/agent-scratch/toad-test-ich92jvs/stdout.log`; the Toad fork owns
that persistence diagnosis and the accompanying retired-session notifications.
Neither attempt establishes the recorded SelectCurrent diagnosis or a deadlock.

The persistence cause is traced: Toad's `Session` declared SQL directives in
dataclass metadata after Core's column compiler moved to `Annotated`. Fresh
session rows therefore had a NULL `id` while insertion returned SQLite's rowid.
The DB family repair is installed: Core `d9ce9cd95` with Toad `a5c627b95`.
Session and model-history rows share annotation-derived schema and the existing
SQLite connection/transaction owner; SQLite errors propagate. The unused second
connection path, success-only bools and aiosqlite dependency are deleted. The
one-time SQLite carry preserved both model-history rows and the existing sessions;
`/home/ts/.cache/agent-scratch/toad-db-before-install.db` retains the backup.
The accepted transient converter is retired. All 194 registered threads stayed
unchanged, with no owner restarts. Actual installed saved-fork/fresh-DM/reply/tab
return and complete native client-request journeys passed: 2 checks in 54.95 seconds,
recorded in `installer-db-batch-native.txt`. This includes reading the newly
created session's persisted title through the installed DB.

The Toad binding fork separately owns the confirmed new-session ordering defect:
the server reuses load's replay-before-response behavior before the client knows
the new ID. Retained wire records 43–51 in `toad-test-ich92jvs` prove seven new-ID updates
preceded their creation response. The repair belongs to the existing subscription
request family; a fresh subscription announces identity before replay, while load
keeps its required replay-before-response contract. Source implementation and an
actual stdio ordering check are in progress; the passing DB journeys do not cover
this packet-order assertion.

### 2. Commander verbs take a fixed, small time

The installer ownership fork measured a cold read on installed Core `a500297f9`:
imports took 1.560 seconds and the refused Say gate took 33.635 seconds, dominated
by decoding history for unit questions and directives. Read phases were
cProfile-instrumented; these are diagnostic costs, not uninstrumented latency.
The independent reference read took 9.887 seconds and returned no references;
it is not added to the refused verb's cost because that verb would stop first.
Source proposal and profile evidence live in
`/home/ts/.cache/agent-scratch/installer-fork-commander-cold-read/`.

PR #2549 already owns `AnsweredSource` and `MessageIdentitySource` projections.
The next change integrates those owners with current commander's question and
directive consumers, preserving newer main's `UnitState`, `NeedsAnswerKind` and
claim lifetime. The installer ownership fork is shaping that integration against
the conflicting virtual-bus batch; no optimization or subsecond result is claimed.

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


### Installer reporting implementation checkpoint; user pause

Draft #2620 now moves publication into the existing RuntimeProbe/ServiceSender family,
selects the runtime that owns each migration stage, and keeps terminal reporting outside
TargetTree cleanup. RunMode owns rehearsal/live behavior while FieldCodec retains the
single Boolean launcher representation. The outgoing report retains its declared probe
source in memory; the incoming report runs its installed module after source retirement.
Probe commands use runtime home instead of the retired checkout. Existing bridge resume
ownership now covers partial pause failure and decodes a broken socket into the existing
per-bridge failure type.

Latest focused report/cleanup tests pass14/14 (22.44s); the preceding command/bridge batch
passed43/43 before retained-source execution was added. These are source checks. Actual
new installer build/rollback/fresh-root qualification and the paired installed journey
remain open. The earlier package-probe test failure from the main editable environment
is retained in the root evidence. Bridge state discovery still misses custom/default
Claude directories; this is an unresolved installer defect owned by root. The full old
nominal bridge-outcome port is not claimed complete.

Tristan explicitly paused the fleet to reopen Codex after its thread tools retained a
dead MCP address. Preserve current dirty owned checkouts and uncertain inputs. The
existing root handoff contains precise heads, results and remaining integration work.
