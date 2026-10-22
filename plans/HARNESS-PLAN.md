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

### 1. One owner for "may I write"

- The lock wait fails within seconds and names the holder's pid and command. A wait that cannot name its holder is a defect.
- Every writer (connector, bridges, hooks, unit workers) takes its "may I write" answer from `MaintenanceBarrier`, so no install has to stop them by hand.
- The hand-spelled `root / "wire"` lock paths go, as `plans/VIRTUAL-BUS-PLAN.md` already orders. One bus lock.
- The transcript lock and every other separate install control are deleted once the barrier covers their writers.

**Done when** `agent-comms install` is the whole procedure on the live bus, and a writer that holds a lock during an install is named in the install log within seconds.

### 2. Commander verbs take a fixed, small time

- Profile one verb end to end on the live bus with nothing else running.
- Cut import cost (load only what the verb uses) and make the answer gate read only the rows it needs, never the whole bus.

**Done when** `order say` and `order board` each answer in under one second on the live bus.

### 3. No silent failures

- The bridge watch waits out maintenance and reconnects; it reports the wait instead of crashing.
- The session relay restarts, or says exactly why it cannot.
- A say that draws a jargon flag either records every answer or is refused. Nothing posts with its answers dropped.
- `--answers` refuses a malformed id (for example a comma-joined list) at the command line.
- A commander write that waits on a lock prints what it waits on.

**Done when** each case above, reproduced once on a practice copy, ends in a named answer or a recorded result.

### 4. Pings, not a firehose

- The heartbeat carries only new decisions, at most five lines, as `plans/commander-native-plan.md` phase 6 describes. Repeated lines and per-message vibe-check ids stop reaching the commander.

**Done when** one hour of the live bus delivers each decision to the commander once.

### 5. Plans that read as one

- Delete the dead files in `plans/`; keep the three live plans and this one.
- Delete `REMAINING-SLOP-PLAN.md` or `remaining-slop-plan.md`, whichever is superseded.
- The multiple inheritance plan keeps one version: its corrections replace the original body instead of sitting above it.

**Done when** `plans/` holds only plans someone is executing, and each states its target once.

## Rules for this plan

- Direct pushes to `main` for this plan only. Every other plan still lands through the merge queue.
- Run only the tests the change touches, locally, in the background under a timeout.
- Each change deletes what it replaces in the same commit.
- Say what is slow or broken the moment it shows, out loud, instead of working around it.
