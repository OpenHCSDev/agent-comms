# S7 TurnRunner and merged ACP deletion closure

PR: https://github.com/OpenHCSDev/agent-comms/pull/161
Implementation head: `668caa34425da89d1b38e2bf1c1b38fd3c83b0c7`. Parent integration
starts at `8cbfec3`; the only later source change is the TurnLeaseFence name.

Owner: Darwin. Worktree: `/home/ts/wt/comms-refactor-s7-turn-runner-20260928`.
Branch: `codex/refactor-s7-turn-runner-20260928`.
Implemented replacement plus the parent's uncaught execution error feedback request.
PR149/154's superseded ACP interfaces are deleted in this candidate too.
Current main through PR153/156/157/158 is integrated. No live state, installed
runtime, configured provider, another worker's checkout, or native bundle was mutated.

## Determining owners and deleted surfaces

- `TurnRunner` owns turn tasks, locks, active IDs, persistent S2 backends, goal
  launch store/accounting/scheduling, cancellation, terminal release and shutdown.
- `OwnedTurn` owns one execution's admission/fences, original input, native send
  boundary, controller, prompt preparation, stream callbacks and finalization.
  Existing S2 `backend.TurnSession` still drives the process/RPC stream.
- `TurnProgress` consumes the existing A10 AgentEvent declarations through A4 MRO
  handlers. It owns observed outcome, usage, goal-attempt accounting and publication.
- SessionLifecycle/ConfigOptions and InputDrain remain the unique session/config
  and queue/admission owners. Their consumers now call those owners directly.
- ACP shrank from 2,934 to 844 lines; CommsAgent from 108 methods/accessors to 26.
  Removed 95 adapter/accessor names listed in `deleted-acp-surface.json`, plus
  `compact_context` and `_sanitized_compaction_summary`. No ACP properties remain.
  The ConfigOptions alias on ACP and TurnRunner was removed too.
- Removed `TranscriptUpdate.from_legacy`, its FieldCodec decode/re-encode, and the
  dict alternative in `_emit_event`. Manual compaction publishes an actual
  StartedTranscriptUpdate; tests instantiate declarations. Saved TranscriptEvent
  projection still preserves existing saved data and external ACP JSON.
- Runtime requests/proxies, manual compaction, compaction publication, config,
  session lifecycle, event projection, subprocess fixtures and in-repo tests use
  the new owners. ACP retains protocol methods and actual protocol/private-route
  effects; it has no shared-self state facade or old turn implementation.

`TurnRunner` imports/annotates TurnLeaseFence directly, ready for PR159 removal
of the old TurnClaimFence alias.

## Failure feedback

Uncaught execution failures, including the adaptive summary hook, publish one
existing `events.Error(str(error))` through AcpEventConsumer before re-raising.
Prior Error or unsuccessful Done publication suppresses a second error, including
when the old per-session Done dedup entry has already been consumed. Failed UI
publication does not replace the original execution exception. Cancellation,
cleanup, original exception causes, UNKNOWN and no-retry semantics remain intact.

Mounted ACP tests prove a selected-summary 402 exception reaches the client once,
includes inputFailed recovery text, starts no original native input, keeps the
original disposition UNKNOWN, releases turn/input state and re-raises the same
exception with the same cause. Separate tests cover prior Error/Done dedup,
concurrent session cancellation and joining turns before session release.

## Local evidence

All pytest runs disable default addopts/xdist and have a 60 or 165 second shell
bound. Source subprocesses use an absolute PYTHONPATH. No CI gate or provider call.

- `verified-core-tests.txt`: 214 passed, 2 skipped (selected native RPC cases need the separate PI_NATIVE_PACKAGE_DIR flag).
- `verified-consumers-0.txt`: 222 passed, 31 skipped.
- `verified-consumers-1.txt`: 173 passed, 38 skipped.
  These two batches cover the migrated caller files. Skips are optional stack /
  prepared native fixture gates, not inferred passes.
- `final-native-tests.txt`: 21 passed, including two actual native SDK/RPC queued
  followups through selected summary, ordinary foreign ingress, adaptive owner
  gates and failure-feedback/component tests. This was before main's PR157 merge.
- `integrated-native-tests.txt`: 28 passed, 2 skipped: actual SDK/RPC queue, selected summary
  transport and feedback checks against PR157's prepared bundle. The two selected
  native RPC cases use a separate fixture flag.
- `nra-final.json`: full src context, all 79 detectors, zero omissions, complete
  exact_compact_global scan, zero reported findings on selected changed files.
  This is structural coverage, not a native semantic-equivalence proof. The
  component synthesis/phase split was authored; no NRA creator-frame proof claimed.
- Ruff and git diff --check pass.

Earlier runs exposed old fixture interception points, module exports and embedded
subprocess attributes, which were migrated. Two inappropriate Comms receiver
rewrites were corrected and covered by the passing caller batches. The model pause
fixture now asserts the existing declared permission refusal; selected-write fake
owners explicitly declare their fake model as required by current S5. Native MCP
fixture dependencies were reused by owned symlinks, never copied or installed live.

## Current Toad callers and parent acceptance

`toad-pilot-callers.patch` changes six test pilots only. `toad-callers.json` records
source revision and each receiver's constructor. All changed receivers are actual
core CommsAgent objects. Toad Agent RPC APIs (including update_goal/edit_goal) stay
unchanged. Earlier wrong UI-receiver substitutions were removed before handoff.
The queue pilot's hardcoded source-file digest gate is removed; behavioral checks
remain. The core external ACP/socket protocol needs no production Toad change.

Parent owns applying and running the paired Toad pilot patch on current Toad main.
Worker did not run the Toad pilots or edit the parent's Toad checkout. Also apply
`parent-acceptance-callers.patch` to the parent's current real-queue harness before
its next fresh-provider acceptance: that harness inspected the deleted backend
accessor. This patch does not rerun old attempts or alter existing receipts.

## Install/acceptance procedure (parent owns activation)

1. Review/merge this complete branch; apply the paired Toad pilot changes. The
   existing installed runtime stays active during isolated candidate validation.
2. Build the normal wheel into a new persistent isolated runtime. Use the current
   PR157 native package and the normal stack preparation/pins. No live package edit.
3. Run the migrated ACP/Toad paths and the parent's fresh-root queue harness.
   Prove accepted_not_started -> linked/committed summary -> original and queued
   native IDs once in order, preserved facts, no UNKNOWN. Never reuse the earlier
   UNKNOWN source or automatically replay input.
4. Activate the complete paired runtime by the parent's normal serial procedure.
   A rollback selects the previous immutable runtime; saved files/journals require
   no destructive migration and this worker changed no live data.

Remaining acceptance boundary: parent configured-provider/installed UI validation
and activation. Source tests and native fake-provider cases do not claim that live
installation has already occurred. Queue/compaction policy and registry identity
meaning are not redesigned by this change.
