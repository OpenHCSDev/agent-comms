# Existing input-family admission owner

Current main87c, draft546. Mendel owns native integration and545 NativeSourceCursor/ProvenSourceCoverage/HistoricalNativeInput; Einstein requested the input/source-admission family handoff before production edits. No new worktree or environment.

## Real source distinction

InputAttempt declares binding and finish_unbound hooks. Only ReservedInput.bind returns BoundUnknownInput; InputDispositions.bind_originals commits all members atomically before the original writer. Only ReservedInput.finish_unbound returns NotSentInput. SentInput/BoundUnknownInput inherit the refusal to finish_unbound, so finish_turn_inputs cannot silently settle a bound uncertain input. NotSentInput remains durable, attention-unresolved and ineligible for new reservation/binding; its existing unsettled_for returnsFalse. StartedInput has original native-start evidence and also does not block source compaction. These are existing family members, not new state labels.

InputDocument.compaction_rows selects original membership, validates pending originals and the process-local FutureInputQueue, and then uses InputAttempt.unsettled_for. Continued-private-source verification instead tests row.unresolved independently, before recorded ancestry. That flag also selects historical user notices and goal review. It is not a native-delivery uncertainty contract. The current original key acp:0d9d2784d0f34858947e138b375e83e5 is still NotSent, oldadmission1205; no original was altered or excluded. Earlier540 actual read explicitly preserved that refusal while separately validating retained ancestry. Current2101/2063 activity retains only the outer coverage-floor error: historical nested traceback is unavailable.

## Coherent implementation decision

Reuse unsettled_for on the existing InputAttempt family. Settled originals and confirmed NotSent do not block. ReservedInput owns the exact captured pending-original exception. BoundUnknownInput blocks even across admission changes or when its key is supplied as pending: naming a bound original cannot grant a resend. InputDocument owns the shared membership/readiness operation used by both its canonical compaction projection and the continued-source verifier. Remove attention-flag and current-admission reconstruction from source-readiness decisions. Keep live FutureInputQueue filtering, source/reservation fences, recorded native proofs, raw UNKNOWN markers and fork publication unchanged.

The reservation/commit/recovery consumers retain AlreadySentRule and NativeBindingExistsRule, selected one-use acknowledgment and original durable bind. Attention and goal-review consumers still legitimately use unresolved; they are different facts. No durable row/format/carry changes, no new class, registry, state, proof cache or codec are required.

## Caller closure

Before AST: existing refactor-audit Package parser maps311 source modules with0parse omissions. Exact lexical calls are preserved in before-ast.json; this is not dynamic resolution proof. The external native SDK receipt/write contract is read directly, with no dependency edits/JavaScript AST claim.

CompactionBoundary.hold and HeldCompaction.capture call InputDocument.compaction_rows/material. SelectedOriginalBinding rechecks that operation immediately before consuming its one-use capability. FutureInputQueue.compaction_inputs projects the same original rows under its existing wire/owner/turn fences. SelectedSummaries.reserve uses that projection when available, then PrivateInputs.require_source_coverage ->verify_continued_private_session. That is the only production caller of the continued verifier and the sole private-coverage reservation entry. All of these must consume one family readiness decision; they must not reinterpret attention notices as uncertain native delivery.

After implementation, batch affected family/continued-source controls and run one coherent configured saved-fork installed journey with Mendel/Sch. Preserve the original failed540 read,79cb and418 input evidence. Never retry0d9; final input must be new. No new environment or independent provider benchmark.
