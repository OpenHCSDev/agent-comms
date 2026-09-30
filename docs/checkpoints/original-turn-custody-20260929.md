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

Implement a resource envelope spanning original goal launch claim, registry lease, input batch/native inbox, preparation/admission, stream, cancel, publication and retirement. Delete TurnGoalAccount.resolved and VerifiedGoalSettlement.recorded and all readers; derive settled outcome from original goal attempt journal once. Retain actual ToolEnd proof instead of successful_tool_observed. Future project continuation derives from actual terminal result and original source, not running-stream followup permission. Initial command capture must transfer the original resource once rather than snapshot/copy pending text across widgets.

Mendel430 identified ordinary routed TurnProgress.publish_result drops original wire send receipts. Close that caller through the existing transcript/provenance owner; no equal-body dedup, independent seen list or new outbound registry. Original actor/source/goal/input identities remain distinct.

Use bounded original-journal and startup/cancel resource checks before one actual installed continuous affected user journey when the existing serial native slot is released. Preserve all uncertain input/native journals/failed traces. No new provider or live replay/interrupt/restart. Existing merged425/211/433/219 and Ready435 acceptance is not repeated. Parent owns merge/live cutover; remaining lifecycle scope is not claimed complete.

Persistent source /home/ts/wt/comms-original-turn-custody-20260929. Disposable output /home/ts/.cache/agent-scratch/original-turn-custody-20260929. Successful435 owned overlays/wheels/test roots removed9,807,460bytes after exact owned process scan returned none; source/committed evidence and live originals preserved.
