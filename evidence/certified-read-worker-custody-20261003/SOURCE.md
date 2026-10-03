# Physical read custody and joined publication

Original560 proof: PID1583616/birth46995415, BUS inode17965242 heldEX
fd11/waitEX fd10; native finalb7807f3e already committed. Stack is final
TurnProgress.publish_result -> transcript_checkpoint -> AssignedTranscriptSource
frontier/window/rows -> WireLog.conversation_sources -> _store_lock.
Original execution, answer, input proofs and journal remain untouched.

The worker had completed but WireLog.read_certified_async kept its original
opened file alive until event-loop result delivery. POSIX release_store_lock
correctly does nothing: inherited physical custody ends at last descriptor
close. A synchronous publication read blocks that same event loop, preventing
the close needed to satisfy its own acquisition. This is resource lifetime,
not provider delay, and does not establish the cause of555's historical13.696s.

WireLog's existing _read_certified now owns the original IO object's close,
after certificate currentness and platform release, before returning detached
observations. Outer IO closure remains the acquisition-failure/cancel cleanup;
closing the same IO object again is idempotent and cannot close a reused fd.
Original worker cancellation joins through close via Coordination.run_worker.
InputDrain's detached observation then opens registry/SQL; no BUS custody leaks.

TurnProgress publishes committed notices and their consumed streamed text in
one joined worker. The original success sends + partial-publication annotation,
failure diagnostic/notices and final checkpoint run as one joined terminal
operation. Only the detached checkpoint crosses into ACP delivery. Cancellation
cannot retire the OwnedTurn lease while a send or annotation is still running.
TurnRunner relay uses the same joined publication; its sequence comes from the
original committed Message instead of reading the global last sequence again.

Native response publication already consumes its acquired source via
LiveResponseOwner/Coordination. TranscriptRoutes.record_turn_publication borrows
its own source for original seq/id, lease, STARTED receipt and annotation commit;
it does not reacquire BUS while that certificate is held. conversation_sources
captures original bytes under its own certificate and decodes after release.
MaintenanceBarrier's async shared ingress and inherited child custody are real
longer lifetimes; neither is weakened. No lock mode, release_store_lock behavior,
lease fence, deadline, catalog, source/store/schema or pending state is changed.

AST uses existing NRA Package over production311, tests361 and tools53 modules.
Before/after output records matching declarations/calls/imports. Attribute names
are candidate references, not proof of dynamic dispatch; no parse omissions.
The original tree's native dependency uses physical inherited custody; no native
module or manifest changes in563. Native562 is a separate frozen checkpoint.

Production batch:20 added/8 deleted lines in3 existing files. Source sanity:
2 changed custody controls passed1.47s. The read control uses real certificates
and lock contention, callback refusal/cancel, and a completed worker while its
loop cannot deliver the Future. Publication control uses real OwnedTurn,
registry/input/diagnostic/message/checkpoint owners with supplied native events;
it proves joined publication/lease ordering, not a provider or native answer.
Installed qualification follows without another provider/input/reproduction.
