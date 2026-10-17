# P169: received coordination revisions

`PiSessionChild.coordination_revision` owns the last received coordination revision. It contains provenance, not instruction text or a per-thread dictionary. `TurnContextObserved` walks the actual SDK contributor manifests; declaration-owned `remember_received` records only coordination contributions whose original byte ranges survived native input conversion. Merely assembling or sending an input does not advance the revision.

`CoordinationSegment.needed_after` compares its original provenance (the existing owner revision and instruction-file revisions) with that receipt. User input and other segment families remain unconditional. No whole-prompt hash is compared and no instruction text was shortened. `InstructionSegment.capture` now owns instruction loading; its callers no longer duplicate that operation.

The existing child forgets the receipt on completed automatic compaction, successful session-writing queries, native preparation/resume, and a project change detected from the original native launch's cwd. Child retirement naturally discards it. No extra LoadedNative state, state-type argument, context cache, timer, reminder store or force-full flag remains.

## Source evidence

Before/after AST census and original measurements are retained under `/home/ts/.cache/agent-scratch/p169-core-read-locks`. `receipt-owner-after.json` enumerates production declarations, writes, decisions, imports and consumers. Native JavaScript contribution ownership was read separately; it is outside that Python census. Declaration-family dispatch is dynamic, not statically resolved by AST.

Owner/consumer searches:

```
rg -n 'coordination_revision|forget_context|remember_received|needed_after|context_for_input' src/agent_comms
rg -n 'LoadedNative|retained_state|state_type' src/agent_comms/{native_custody,native_session_prepare,backend,tracked_turn,pi_native_backend}.py
```

## Actual native receipts

An independent private ACP owner opened the completed saved-session fork, used the ordinary input reservation, OwnedTurn, backend, native process and SDK, and received controlled localhost provider responses. These are actual captured provider payload bytes, not reconstructed-manifest projections. They are not installed/configured-provider acceptance.

| Prompt | Unfiltered current builder | Retained input actually received |
|---|---:|---:|
| `hi` | 768 | 296 |
| `Continue the shared context repair` | 1539 | 919 |

Direct segments: automatic title 294, coordination 472, user input 2. Collaboration: automatic title 294, coordination 620, user input 34, relationships 591. Only unchanged coordination disappears. Changed collaboration resends all 1539 bytes. First turn sends all 768. After actual native compaction, all 1507 bytes are sent; after the real project setter, all 1513 bytes; after closing/reopening the native child, all 1513 bytes. Private provider request bodies confirm those totals. The title segment is retained deliberately rather than deleting instructions to improve the numbers.

`boundary-final-results.json` and `boundary-final.log` retain the receipts and retired-child report. An earlier candidate omitted coordination after an idle project change; `project_resume_due` only covers an executing turn, so selection now checks the launch-owned cwd too. Earlier permission/startup failures and the failed candidate remain retained. No failed or UNKNOWN input was replayed. The immutable source cut remains unchanged.

The existing comparison batch produces exactly the same 20 failure lines on current main and this branch, with 22 passes and 7 skips in each run (`main-checks.log`, `feature-checks.log`). The failures include absent immutable native-package configuration, obsolete context inspection APIs and installed-only assertions. These are comparative observations, not assertions that those failures are harmless. No new tests were added.

The old 502→2 and 1820→34 numbers were synthetic projections from the superseded prototype and are not acceptance evidence.
