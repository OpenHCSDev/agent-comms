# Original turn resources and journal settlement continuation

## Separate failure consumer checkpoint

Core436 `f955ee52fa702d95192cc297e926caa9e9ccba28` consumes the original exception rethrown by Einstein434. TurnProgress writes its private traceback to the existing turn diagnostic before attempting a safe public Error. OwnedTurn joins the original native child even if diagnostic or public failure delivery raises. Production changes: 3 lines deleted, 16 added; the existing real-owner fixture verifies private cause retention after transport disconnect, no private error text in public events, and no replay.

Exact committed source, detached proof worktree: one focused test passed in 0.97 seconds:

```sh
PYTHONPATH=src:/home/ts/.cache/agent-scratch/compaction-output-accounting-20260929/test-deps \
  /home/ts/.local/share/agent-comms/runtime-canonical-native-checkpoint-20260929/bin/python \
  -m pytest -o addopts= -q \
  tests/test_s1_event_behavior.py::test_failure_publication_preserves_private_cause_after_transport_disconnect
```

The changed-source debt ratchet against `42c225aa` exited zero with no positive deltas. This is source verification, not an installed native gate. Sch215 owns the serial affected installed journey and received this consumer checkpoint. No provider call, live interruption, replay, installation or owner restart was performed here.

The rejected nullable-handle reconstruction and nested retirement finally blocks are absent from this commit. Remaining resource acquisition must register cleanup at acquisition with existing scope/callback conventions; goal settlement precedes lease idle publication, and failure joins remain mandatory. IMPL-14, IDEN-3 and IMPL-10 require the original native result to own continuation capability instead of a consumer boolean-plus-terminal chain. The goal journal cleanup and initial UI command custody below remain unfinished and are not included in the separate checkpoint.

Arendt continues full T4/S14 after Ready435 original retained restart batch. This continuation owns OwnedTurn acquisition/retirement, TurnGoalAccount original journal settlement and its ToolEnd witness, TurnProgress original terminal publication, and initial UI command custody. No new lifecycle authority or copied status. Mendel owns GoalActions/GoalStates and Transcripts original outbound source join; Einstein owns native TurnSession/event decoding and native writer434; Heisenberg217 owns retained session restoration/navigation. Request existing public capabilities across those boundaries instead of probing private fields or storing mirrors.

## Acquired resource continuation, still Draft

The working resource checkpoint uses ExitStack and AsyncExitStack callbacks registered immediately when the original launch permit, registry lease and input inbox are acquired. After stream binding, `pop_all()` transfers the acquired permit scope above inbox retirement. Every native run registers child joining above that transferred scope. A lease custody scope publishes project continuation immediately before its exact idle settlement. Retirement order is child join, goal account settlement and claim discharge, project child close, inbox retirement, successful original Done continuation, then lease idle publication. Callback exceptions do not skip lower acquired resources; no nullable handle discovery or nested retirement finally blocks remain.

Deleted semantic copies are TurnGoalAccount.resolved, VerifiedGoalSettlement.recorded, successful_tool_observed, TurnProgress.outcome/native_terminal, OwnedTurn.goal/turn_admission/current_project, and the old monolithic finish. Productive work keeps the actual successful ToolEnd; verified settlement rereads the original attempt journal through LaunchPermit.has_verified_progress, including after a successor generation advances. StreamSettled still publishes the native output/statistics boundary without becoming stored completion proof. The actual Done owns project_continuation; InputDrain applies its own live queue/project eligibility. Thread.active_goal and continuation_goal derive original Goal declarations and original captured project/goal, replacing repeated current-thread activity predicates in OwnedTurn, TurnGoalAccount, scheduler, standby and contact readers. Mendel granted these declaration and consumer edits; mutation still uses original Thread.goal.

Bounded source verification: 10 passed, 1 skipped in 2.34 seconds before the final reader-only migration. Four original claim/lease/inbox/goal-retirement fault points use actual registry, goal journal and input owners and show blocked journal admission, no leaked lease/inbox/source, no new scheduled work and refusal of a new ready grant. An inherited PausedGoal case verifies that the original journal settlement needs no new consumer roster. These checks inject failures at owned acquisition methods; they are not an installed native/ACP/UI acceptance claim.

Remaining mandatory closure: integrate current main normally; consume Mendel's pending Transcripts.record_turn_publication with original StartedInput and returned MessageReference receipts; repair obsolete S1 fixture expectations through existing actual producer APIs; transfer initial UI command custody; run one affected installed continuous native/ACP/UI journey after Sch's serial slot is released. The broad resource checkpoint stays Draft. Separate f955 failure publication is already in Sch's real failed02 trace and exposed its actual pre-byte Busy/native_unknown cause. That original remains failed and is not replayed. Core438 independently owns the typed batch cutover seam; its default and authorized maintenance operation must remain member behavior, with no raw callback policy or same-wire-lock reentry.

Implement a resource envelope spanning original goal launch claim, registry lease, input batch/native inbox, preparation/admission, stream, cancel, publication and retirement. Delete TurnGoalAccount.resolved and VerifiedGoalSettlement.recorded and all readers; derive settled outcome from original goal attempt journal once. Retain actual ToolEnd proof instead of successful_tool_observed. Future project continuation derives from actual terminal result and original source, not running-stream followup permission. Initial command capture must transfer the original resource once rather than snapshot/copy pending text across widgets.

Mendel430 identified ordinary routed TurnProgress.publish_result drops original wire send receipts. Close that caller through the existing transcript/provenance owner; no equal-body dedup, independent seen list or new outbound registry. Original actor/source/goal/input identities remain distinct.

Use bounded original-journal and startup/cancel resource checks before one actual installed continuous affected user journey when the existing serial native slot is released. Preserve all uncertain input/native journals/failed traces. No new provider or live replay/interrupt/restart. Existing merged425/211/433/219 and Ready435 acceptance is not repeated. Parent owns merge/live cutover; remaining lifecycle scope is not claimed complete.

Persistent source /home/ts/wt/comms-original-turn-custody-20260929. Disposable output /home/ts/.cache/agent-scratch/original-turn-custody-20260929. Successful435 owned overlays/wheels/test roots removed9,807,460bytes after exact owned process scan returned none; source/committed evidence and live originals preserved.
