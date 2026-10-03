# Working source checkpoint, not Ready

106 production lines deleted /171 added in nine files. Four original consumer setups
delete repeated admit/begin/prompt/open sequences and use OwnedTurn.acquire. This
batch uses original Coordinator worker joins and stack lifetimes; no new class,
registry, queue, cache, state declaration, provider policy or native method.

## Source fact and owners

OwnedTurn.run formerly performed registry/goal admission, transcript checkpoint,
bus-input registration, InputBatch capture, native environment and prompt roster
reads synchronously on the event loop. open_stream then performed durable input
reservation among its loop-owned Queue/Event/controller operations. Goal permit
retirement, current project reads and failed-input wire transactions also blocked
that loop. Source proves these synchronous lifetimes; recorded4.480s is not an
attribution or a speed comparison. PeerState consumes roster status/activity;
thread_views reads runtime/goal resources, not a full native journal or bus scan.
No speculative roster cache or parallel snapshot authority is introduced.

OwnedTurn.acquire keeps current_task, session bindings, events, controller and
queues on the event loop. Original admit and begin/source/reservation run through
Coordination.run_worker. begin registers the existing lease and original-input cleanup before worker
delivery, then captures the original prompt and OriginalTurnInput reservation.
Early failures retire permits before inbox/lease; after successful stream opening
the same permit custody transfers above inbox/lease. Goal settlement retains its
original order and original grants. Existing InputBatch/TurnInputSource decisions
are moved intact, not copied or weakened.

InputDocument owns reservation membership. Both InputDispositions.record and
reserve_turn consume that same behavior. reserve_turn enlists finish_unbound on
an existing ExitStack before publication and returns SingleInputBatch from the
exact document returned by LockedStore.update. OriginalTurnInput no longer
performs a second read after recording. OwnedTurn retains the original wire scope
until the updated batch and keys belong to its outer input custody. An error
capturing the receipt or transferring custody therefore closes the same newly
reserved original; duplicate reservation refusal never enlists an existing row.
The store's original fsync/atomic rollback behavior remains unchanged.

An original AsyncExitStack retains the reserved input's rollback until open_stream
installs that same OriginalTurnInput on InputDrain. Cancellation before worker
delivery therefore cannot orphan a Reserved input. Successful ownership transfer
removes that rollback; it adds no flag/cache or independent input authority.
The containing stack calls aclose directly. Once ownership transfers, the empty
stack does no worker submission; only actual rollback enters the joined worker.

Original resource callbacks retire goal permits in a joined worker. Project
identity is read off-loop while project continuation remains on-loop. Cancellation
and failure consume the same original unbound-input transaction; its optional
homogeneous-state projection is the existing InputDocument observation, not a new
None lifecycle state. UNKNOWN remains uncertain and cannot grant input/replay.

The subsequent pre-native stage also joins its original phase read and canonical
awareness acquisition; awareness still owns opportunistic unavailable handling.
Adaptive-compaction failure uses the same goal owner in a joined operation.
TurnRunner.prepare_selected_session now acquires its registry-derived native
environment in the worker, covering original context/manual/owned callers without
changing NativeSessionPreparation or a native method. Its loop-owned persistent
child remains in the original async preparation lifetime.

InputDrain.finish_turn_inputs serves owned and selected turns. Its original input
is removed/settled under the same wire cut in a joined worker; finally retires
remaining loop capabilities, burns selected admission and preserves queued inputs
even when cancellation is rethrown after write completion. Publication follows
that completed resource retirement. Queue/controller resources remain resources;
no copied lifecycle status is added. GoalAttemptStore owns per-operation SQLite
connections, so no open connection crosses worker boundaries.
Retirement consumes original.keys rather than original.notice_keys: notice
membership is a display fact, not custody. AcpEventConsumer.on_done no longer
mutates input storage; its duplicate wire transaction is deleted. Its existing
failure publication remains. InputDrain's original turn-resource callback closes
the input owner on success, failure and cancellation for owned and selected turns. Existing InputAttempt.finish_unbound
owns each disposition; no UNKNOWN is replayed or treated as known-not-sent.

SessionLifecycle.sync_identity joins one original RegistrySnapshot and consumes
both identity and status from that cut. RegistrySnapshot now owns the existing
status alias lookup/unregistered refusal; Registration.status derives it rather
than maintaining that lookup separately. All seven sync_identity callers use the
same method. SessionLifecycle.metadata joins original registry/goal/runtime-info
projection before its loop-owned queue/cursor/client effects. It returns existing
typed updates, introducing no observation cache or new lifecycle state. Legitimate
optional runtime usage remains optional; storage errors still propagate.

## Consumer closure and limits

Original NRA parse_python_module_roots covers src/tests/tools:726 modules and zero
omissions before/after. Relevant syntactic declarations/references are retained
in source-before.json and source-after.json. Partial callbacks, MRO and the stdlib
ExitStack/asyncio resource semantics were read; syntax is not dynamic proof.
Production entry is TurnRunner.run_agent_turn -> OwnedTurn.run -> acquire. Four
direct setup consumers migrated: atomic source attachment, private idle/publication,
S1 effect fixtures and channel awareness. The channel fixture now observes its
actual rendered Context while original task intent remains unchanged; it no longer
expects preparation to mutate that task or bypasses original stack retirement.

Supplemental before/after NRA evidence covers the reservation/resource family
(12 declared modules) and session/status/publication family (11), with no parse
omissions. All reserve_turn consumers are migrated; its one production caller is
OriginalTurnInput.reserve, consumed by OwnedTurn.reserve_input. Inheritance and
callback execution require semantic reading; AST cannot prove dynamic resolution.

All 14 changed Python files compile and git diff --check passes. No behavioral,
installed or provider qualification is claimed yet. Final controls must address
cancellation after source/permit commitment and input cleanup before publication;
The existing goal-resource failure control now includes the concrete post-write
receipt-capture failure and checks that the original row remains NotSent. It has
not run yet. The affected configured path comes last on a released existing holder.
The unrelated pre-existing test_coordination ExecutionRecord(exact_target=...)
fixture debt reported by parent601 is recorded, with no production workaround.534 is now
exclusive receiving401 package code;485 is LIVE. Neither is borrowed here. Original
595/597 private buses, saved sources, proofs/auth/UNKNOWN and negative receipts
remain protected. Original13.562/13.696/98.141/99.101s gaps remain unclosed.

Pattern relationship: IMPL-12/13 and lifecycle ownership; shared acquisition and
retirement carry the work, replacing consumer choreography rather than introducing
another wrapper or store.
