# The harness: make commanding playable again

Decision (Tristan, 2026-10-21 22:45): the commander fixes the harness it commands with. It works with full authority on this plan only: it makes each change, inspects it with full attention, runs only the tests the change touches, and pushes straight to `main`. Audited against `origin/main` at `39fdfe810`.

## Why

On 2026-10-21 the plans Tristan asked for at 18:30 the day before had not landed 28 hours later. Every error pulled the fleet off them, and the errors came from the harness itself:

- Install 52 sat for twenty minutes holding the wire lock and waiting on the registry lock (`maintenance_barrier.py:380`, `store_files.py:157`), sleeping in short naps and never naming the holder. The live coordination store was found malformed in the same minute the install was stopped.
- "May I write during an install" had five answers: the maintenance barrier, a transcript lock, a connector stop, a bridge watch stop, and the commander's shell wrapper around `agent-comms install`. Each install failed on a different missing one.
- One commander verb took one to five minutes under load. About 4.8 seconds of each call was import alone (2,093 dataclass builds, 433 pydantic models), and the answer gate then scanned the bus.
- Failures were silent. The bridge watch crashed when the socket moved during the install (`cli_bridges/monitor.py:260`, FileNotFoundError). The session relay would not restart ("Session bridge startup not observed ... No launch retry"). Commander writes hung until a timeout killed them, with no output. A say that drew a jargon flag posted but recorded none of its answers.
- About 120 bus events in 30 minutes reached the commander's main loop, including six-part heartbeats listing twenty vibe-check ids.

A silent or slow tool teaches the commander to work around it, and the workarounds are new copies of facts. The fix is at each owner, once.

## Steps, in order

### 1. The live stores stay whole, and lock waits name their holder

- `CoordinationStore.observed()` (`coordination_database.py:220`) and `is_database()` (`:118`) open a plain handle on the live database inside processes that hold SQLite locks on it; closing that handle drops the process's locks. This is the likely cause of the 2026-10-21 corruption (fork AL's forensics). The required answer is what sits at the path (missing, not a regular file, not our database, our store); the store's own read-only connection answers it, so the header read is deleted. Callers: `transcripts.py:397`, `coordination_tables/refusals.py:71`, `notification_assignment.py:350`, `installer/backup.py:271`.
- Installer backup and restore (`installer/backup.py:271-278`, `:360-366`) take the store's own lock instead of relying on writers having been stopped.
- The store lock wait (`store_files.py:157`) fails within seconds and names the holder's pid and command.
- The bus wire lock and its hand-spelled paths belong to the virtual bus batch (#2549), which deletes them; this plan does not touch them.

**Done when** no code outside a store's owner opens its live file, and a lock held during an install is named in the install log within seconds.

### 1b. A head can be judged for a plan phase, not only for an order

`judge head` (`commander/judge.py:166`) accepts only an order the commander holds. A PR that serves a plan phase directly (claude-ai's #2601, the phase-evidence PR #2595, commander fixes such as #2548) can never get the `agent-comms/review` verdict, so the merge queue's review-verdict job refuses it forever; the only way through was the admin bypass. The gate is right; the verdict knows one kind of purpose too few.

- What a reviewed head serves is a family: an order (today's `ReviewedHeadVerdict` on a Todo) and a plan phase (a `PlanPhase` declared in `plan_phases.py`). `judge head` takes either and records the verdict through the same Core tool, which posts `agent-comms/review`.
- No new string or roster names the purpose: the phase is found by its declared name in the `PlanPhase` family.

**Done when** `judge head ReviewQueuePhase SHA --serves "..."` posts `agent-comms/review` on that head and the queue's review-verdict job passes it.

### 1c. Installs never let a second runtime change the live stores

Found 2026-10-22: a store writer reconciles any schema difference on open (`coordination_database.py:262`, `typed_table.py:1812`, `goal_history.py:156`, `cli_bridges/shared.py:170`), so any newer-code process that opens the live bus migrates it, outside maintenance and before the backup. Install 53 left the live coordination store on the new schema and aborted; the old runtime then refused the store and every unit failed to restart.

- `reconcile_schema` takes the writer, and inside its existing schema-differs branch asks `writer.require_schema_change()`: allowed only while this process holds the bus's maintenance hold (`MaintenanceStatus.require_held_by(holder)`: the open member refuses, a held member compares nonces). One rule for every StoreWriter; fresh empty stores still create their schema.
- The installer reconciles each declared store under the hold, after the backup, with the staged runtime, so owners start against stores that already match.
- Step dry runs and `before_stop` run on the practice copy and under the hold, never on the live root before it.
- `maintenance_barrier.py:452` `ingress_async` stops holding the shared wire lock across its caller's awaits (the open test `test_no_wire_lock_scope_or_yielded_caller_contains_await` names it); held across waits, the fleet's shared locks starved installs 52 and 53 of the exclusive lock.

**Done when** a newer runtime opening the live bus outside an install fails loudly naming the store and the hold it lacks, and an install with running units completes without anyone stopping them by hand.

### 2. Commander verbs take a fixed, small time

- Profile one verb end to end on the live bus with nothing else running.
- Cut import cost (load only what the verb uses) and make the answer gate read only the rows it needs, never the whole bus.

**Done when** `order say` and `order board` each answer in under one second on the live bus.

### 2b. The growth alarm counts code, not imports

The growth alarm is commander-native phase 7's (`plans/commander-native-plan.md`, Growth alarm); this step only fixes its measure. Today three places count raw lines (`git diff --stat`, the alarm, PR bodies), so a factoring that turns 22 declarations into one reads as +18 when twelve of those lines are top-of-file imports. One AST-based measure drops the leading module-level imports, blank lines and comments (imports below the top still count), the alarm and PR bodies read it, and each report shows it beside the ratchet's fact measures.

**Done when** the alarm is the only place that counts net source lines and it ignores top-of-file imports.

### 3. No silent failures

- The bridge watch waits out maintenance and reconnects; it reports the wait instead of crashing.
- The session relay restarts, or says exactly why it cannot.
- A say that draws a jargon flag either records every answer or is refused. Nothing posts with its answers dropped.
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
