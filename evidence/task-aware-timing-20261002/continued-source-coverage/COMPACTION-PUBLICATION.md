# Pre-turn compaction observations use the existing output resource

Actual configured202 completed the original authored task, one optional journaled commit and exactly one answer on its new SDK fork in205.96s. It failed continuous ACP progress:9real packets contained no compaction event. Original receipt/inputs remain unchanged; all owned workers retired and configured source hashes matched.

SavedSelectedSession.prepare_context already lends participant.dispatch to the original owner compaction operation. SelectedParticipant had no handler for CompactionEvent, so its existing MRO dispatcher discarded that entire event family. TurnRunner.run_selected did not pass its acquired TurnEffects publisher to SelectedExecution.run.

The same original event now travels through the acquired output callback: TurnRunner.run_selected → SelectedExecution.run → SelectedParticipant.select → its MRO CompactionEvent handler → existing TurnEffects/AcpEventConsumer. The original ACP turn phase owner and encoder do the work. No event is reconstructed, no phase/progress state is copied, no registry/store/timer/replay policy is added. Foreground callers with no ACP transport remain transport-free. The nullable callback is a bounded output resource, not semantic state.

Before AST uses the existing NRA Package/Repository parser for all declared Python roots, with parse omissions and native/dynamic limits explicit. End source checks cover the whole event family identity and the existing selected source lifecycle. One distinct new authored-task/optional-compaction/input journey can continue the preserved202 private fork through existing continuation mode after coherent installation; no old input replay or repeated mandatory compaction is needed.
