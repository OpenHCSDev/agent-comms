## Source owner closure

21 production lines deleted / 46 added in two existing owners:

- DurableTurn consumes its existing nominal observation handlers through Coordination.run_async with a scoped worker AttemptStore. Original object/fence remains the authority; no caller connection crosses threads. Nested phase dispatch stays in that same resource lifetime. Unhandled events open no connection.
- Completed backend Settling/finished/dead/progress is recorded in one existing AttemptStore transaction instead of two revisions.
- ChildProcess owns one shared physical plan driver. Async retirement joins it in the existing executor before event-loop transport release/reap; synchronous retirement uses that driver with its original guard. PID birth/group checks, physical scans and all deadlines unchanged. No process snapshot cache.

## Installed checks

Normal wheel installed with declared dependencies; all311 production files match. Six bounded controls passed: real SQLite EX contention with event-loop release and joined cancellation/exact fence; real defiant child trees, repeated cancellation, birth mismatch protection, synchronous exception/pipe custody. Actual scan instrumentation delegates original OS operations and shows scans run outside the event loop. Six recorded tree identities absent after cleanup. Installed CLI help passed. No native build/provider/native input/public mutation.

Four initial fixture setup errors were missing owned scratch parent only (bodies not entered); output preserved. Two initial passed controls were not repeated; remaining four passed in8.08s.

Evidence: `evidence/owned-attempt-observations-20261002/READY.md`, installed source proof, original child receipts and before/after declaration/consumer AST. All311 production +53tools +357tests parse without omissions; dynamic/external coverage limits explicit.

## Actual385 limit

This closes proven synchronous owner/resource work, not a measured attribution of385's2–14s post-native gap. Public38–49s reply latency is not claimed fixed. Parent owns next paired installation and actual configured-channel acceptance; original385 and all UNKNOWN/proof histories preserved. S3527 remains parked.
