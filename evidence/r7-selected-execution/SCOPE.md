# R7 ownership receipt

Base main199 `065a4da`; own persistent tree/branch
`comms-refactor-r7-selected-execution-20260928` /
`refactor/r7-selected-execution-20260928`.

1. `SelectedExecution` replaces `run_one_sealed_claim`: selection, exact lease,
   sealed source, session/prompt, triage/promotion/full result, durable progress,
   publication and cleanup belong to the same execution object. DurableTurn is
   the sole evolving attempt fence. MutationStore and native admission keep their
   existing transaction/one-use authority. Migrate ACP/cohort foreground and tests.
2. Rename attention declarations/state/API to assignments, keeping resource
   claims as claims. Preserve existing SQL/native spellings at decode/encode
   boundaries. Internal admission/owner epoch names become generation without
   changing any counter meaning or value.
3. Delete recovery ProjectionRecord forwarding and migrate codec consumers.
4. Preserve Pascal R6 files; any unavoidable caller seam will be reported to
   parent/Pascal. Current R6 production write set does not include selected
   runtime, assignment declarations or native prompt/source cursor owners.

No provider calls, native rebuild, live mutation, extra workers or new executor.
Parent owns current Toad consumers, installed acceptance and rollout.
