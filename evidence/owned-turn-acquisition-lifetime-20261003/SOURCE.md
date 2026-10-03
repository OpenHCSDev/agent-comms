# Working source checkpoint, not Ready

61 production lines deleted /100 added in three files. Four original consumer setups
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
Coordination.run_worker. begin registers the existing lease cleanup before worker
delivery, then captures the original prompt and OriginalTurnInput reservation.
Early failures retire permits before inbox/lease; after successful stream opening
the same permit custody transfers above inbox/lease. Goal settlement retains its
original order and original grants. Existing InputBatch/TurnInputSource decisions
are moved intact, not copied or weakened.

An original ExitStack retains the reserved input's rollback until open_stream
installs that same OriginalTurnInput on InputDrain. Cancellation before worker
delivery therefore cannot orphan a Reserved input. Successful ownership transfer
removes that rollback; it adds no flag/cache or independent input authority.

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
membership is a display fact, not custody. Existing InputAttempt.finish_unbound
owns each disposition; no UNKNOWN is replayed or treated as known-not-sent.

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

All seven changed Python files compile and git diff --check passes. No behavioral,
installed or provider qualification is claimed yet. Final controls must address
cancellation after source/permit commitment and input cleanup before publication;
affected configured path comes last on a released existing holder.534 is now
exclusive receiving401 package code;485 is LIVE. Neither is borrowed here. Original
595/597 private buses, saved sources, proofs/auth/UNKNOWN and negative receipts
remain protected. Original13.562/13.696/98.141/99.101s gaps remain unclosed.

Pattern relationship: IMPL-12/13 and lifecycle ownership; shared acquisition and
retirement carry the work, replacing consumer choreography rather than introducing
another wrapper or store.
