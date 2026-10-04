# Original C4 source reconciliation

Audited merged Core `5c1103077e5a982365a6d801b98bcbdb1b03c547`, including
#655 and #656. **Original C4 static ownership/deletion requirement closed.**
No further concrete source family was found in this C4 reconciliation; the
original default UI/live workflow remains parent-owned and pending admission.

## Original requirement and whole root

`docs/refactor/cleanup-20260929/C4-god-classes.md` requires every production
class except **WireLog** to stay within500 lines, state-owning changes instead
of carved mixins, and a reduced edit count for new cases. Its original
HistoryViews/CommsAgent continuation and historical6feb closure are preserved;
the newly grown InputDrain required actual ownership repair rather than
repeating that old closure.

The **original** NRA `Package.load`/`GodClass.detect` parsed all316 production
modules at the merged SHA, zero omissions. InputDrain486, HistoryViews464,
CommsAgent401, CursorPublication106, CursorDelivery20. **WireLog609 is the only
class over500** and the explicit original exception; no new exemption or
measurement relocation was introduced. See `C4-MERGED-SOURCE-CENSUS.json`.

## Facts, storage, lifetime and complete related consumers

| Fact | Existing owner and storage/lifetime | Consumers and removed decisions |
| --- | --- | --- |
| Durable reservation and original disposition | InputDispositions.record_originals/reserve_originals, LockedStore published InputDocument; rollback enlisted in original ExitStack before publication. InputAttempt.finish_unbound owns NotSent versus native-bound/Started/UNKNOWN. | QueuedInput.capture and OriginalTurnInput.reserve take the exact returned document. InputDrain.accept_followup and both TurnRunner initial paths use the same reservation lifetime. Removed bool receipt, post-write whole-document reread, second strict reservation decision and duplicated rollback windows. |
| Live accepted input and initial transfer | QueuedInput.reserve owns wire/worker/custody/capacity; InitialInput.run/finish owns pre-dispatch binding through terminal cleanup. | TurnRunner initial command/channel paths, InputDrain follow-up ingress, original NativeBackendFixture.original_input and every direct/embedded retired run_owned_input consumer are migrated. Existing after.json records declarations/calls; source review includes the embedded hard-exit child. Removed run_owned_input and pending_followups, with no forwarding compatibility path. A new initial caller invokes the one lifetime; it does not reproduce reservation plus finally cleanup. |
| Live source membership | OriginalTurnInput.batch.keys and AcceptedFollowingInput.keys own the captured inputs; InputDrain.input_keys derives their union. | OwnedTurn establishes its original source; OwnedSendAdmission, TurnProgress/AgentEventConsumer completion, FutureInputQueue and selected compaction use the same live sources. Removed turn_input_keys mirror and every producer/clear/refusal/retirement write. Adding a source member no longer requires synchronizing a second set. |
| Pending display versus retained source | queued_inputs/restored_inputs are live QueuedInput grants. following_sources persists accepted provenance after input-start paint; original_sources is the acquired turn source. These are different lifetimes, not durable delivery proof. | InputDrain clear/start/refusal/finish_turn_inputs, QueuedInput source/handoff/restore hooks, OwnedTurn, OwnedSendAdmission and TurnRunner selected/ordinary inbox paths. Clear/start retires pending display, not source proof; refusal/terminal burns the corresponding live grants. Durable UNKNOWN cannot construct a live queue. |
| Owner scheduling/observation | InputDrain owns drain_tasks/wake_tasks, per-session drain locks, pending ScheduledTurn work, closing flag and completed-idle revision observations. Selected-summary admission is an acquired capability invalidated at terminal. | CommsAgent constructs the owner; GoalScheduler/ScheduleCheck derive live wake work; TurnRunner/OwnedTurn attach and join inbox resources; close cancels and joins producers. _idle_private_revisions only suppresses unchanged completed observations and never grants delivery/replay. No duplicate task owner or new scheduler/store was created. |
| Queue and ACP publication | QueueProjection.capture owns ID/text/UTF-8/size admissibility; QueuedInput owns captured-admission participation. AgentCommsUpdate.acp_chunk owns the original empty ACP envelope. Queue revisions remain transport ordering. | InputDrain's queue/start/delivery publishers and session replay metadata derive these owners. Original clear/prompt/promote, SteerPromptRequest and ConfigOptions' SettingCommand.to_rpc all use the existing dictionary inbox contract; retired raw-string-to-ScheduledTurn conversion is deleted. Direct backend string API remains a distinct external contract. New update members inherit the envelope once. |
| Historical C4 cursor publication | CursorPublication/CursorDelivery alone own attached transport observation and successful-publication bookkeeping. NativeSourceCursor/CursorOwner own durable proof. | CommsAgent, SessionLifecycle and InputDrain consume effects.cursors; no retired cursor helpers or parallel CommsAgent cursor maps returned. No durable cursor or input authority moves into presentation bookkeeping. |

The remaining InputDrain fields are original resource bindings or these distinct
live lifetimes: comms/sessions/runtime/effects, dispositions, configuration,
queue/source grants, inboxes, scheduler tasks and observations. They are not
independent authoritative copies of registry TurnState, native receipts or
persisted delivery. `before.json`/`after.json` parsed316 production and365 test
modules with zero omissions, including the original reservation/dispatch
consumer family. Current source reads followed the related field writers in
acp, owned_turn, turn_runner, owned_send_admission, queued_input,
turn_input_source, agent_event_updates, goal_scheduler and schedule_rules.
Lexical AST references plus semantic reads do not prove arbitrary runtime
monkeypatching/dynamic resolution; no zero-by-omission claim is made.

## Deletion and evidence limits

#655 deleted64/added66 production lines across six existing modules, including
the source-key mirror and unused inbox conversion. #656 deleted95/added114
across four existing modules: shared reservation/initial cleanup is load
bearing; no new classes or mixin carving. Parent #654 independently closes the
HistoryViews464/CoordinationSnapshot/MessageNotification source family.

Existing installed evidence is reused, not rerun: #655 eight changed cases
9.36s; #656 original01 **wholeFAIL6PASS1FAIL14.192s**, followed by the separate
corrected readmission02 **1PASS0.878s**. All seven distinct #656 cases are
qualified cumulatively; original01 log/XML hashes are unchanged. Actual SDK
source custody had0posts/0proofs, actual native/socket steer2localhost posts/
3proof rows. Owned controller/native groups and sockets retired,347 installed
assets and92 keepers/ten distributions unchanged. Bohr independently closed
both purposes; no runtime/package/artifact loan remains. No public/provider
speed/UI/full-goal claim follows from these scoped controls.

**Source closure is complete; the current default saved-history/admission,
queue/status, live input delivery/handling and original presentation workflow
still requires parent's affected installed/public acceptance.** Historical6feb
UI evidence is historical, not current verification. No new runtime, check
matrix, model input, package build, scanner or public action was performed for
this reconciliation.

## Retained artifact handoff

The ONE original normal wheel is retained at:
`/home/ts/wt/comms-goal-ledger-schema-carry-20261002/.artifacts/c4-queued-reservation656-wheel-20261004/agent_comms-0.1.0-py3-none-any.whl`

SHA256 `5f1667ce64c58d67e153850be91dfb669ea9e347218f8d845937a1c12c778a9c`.
`wheel-source-proof.json` contains all347 Git/local/ZIP-equal assets, metadata
and the three original Hatch forced resources. Native manifest086/tree dbc88
is source-declared; no native rebuild/copy. Build c656, qualified5bd and merged
5c110 have zero differences in src/stack/.pi/pyproject.toml. Sch may reuse this
standalone artifact for matched receiving under his own purpose. This handoff
implies no holder, public cutover or new execution grant.
