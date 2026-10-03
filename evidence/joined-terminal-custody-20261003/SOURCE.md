# #597: original turn cleanup in the joined worker

AgentActivity still owns begin/release CAS and the exact RegistryOwner/
FinishedTurnFence; GoalManagement still owns whether an original waiter may
release. TurnRunner now owns registering the returned lease/fence on its existing
AsyncExitStack BEFORE worker delivery. Coordination.run_worker joins the blocking
operation and closes its storage resources before publishing to the client.
Cancellation may commit a CAS yet suppress result delivery: the registered
cleanup still runs. A publication exception still releases the original waiters.
An old lease cannot clear a replacement, including a reused turn ID.

All three TurnRunner acquisition consumers use acquire_turn: manual compaction,
relay, and OwnedTurn. Manual acquisition plus invalidation of its old usage sample
is one joined storage operation; relay uses the same owned acquisition. OwnedTurn
uses the shared lease callback registration; its broader admission/prompt setup
is unchanged and is NOT claimed offloop. SelectedParticipant already finishes its
lease in its original coordinator worker and remains there. No lock crosses
awaited client publication. Original native goals/source/input UNKNOWN semantics,
compaction result invalidation and turn task identity checks remain.

Removed finish_turn_stream (a forwarding layer), inline manual/relay cleanup,
OwnedTurn's copied lease callback, and redundant terminal thread-name/turn-id
arguments. The exact lease owns those identities. Existing terminal test consumers
now observe TurnTranscriptUpdate and the real registry CAS rather than retired
active_turns/TurnSettled/stream-settled flags. No new queue/cache/state/deadline,
semantic store, protocol family, native change or provider call.

Before/after uses original NRA parse_python_module_roots across src/tests/tools:
726 modules, zero parse omissions; 236 before / 251 after related syntactic sites.
After includes the new existing-owner methods. AST does not prove dynamic dispatch;
callbacks/MRO and original source boundaries were read semantically.

Source sanity: five controls passed in .77s: successful/failed publication order,
replacement lease preservation, cancellation after actual begin CAS, cancellation
after actual finish CAS. They use real isolated registry/activity/goal owners;
client effects are observers. Explicit PYTHONPATH source sanity is NOT installed
acceptance. First run rejected unavailable pytest-xdist before collecting tests;
its original log is retained. Final normal-wheel installed controls await release
of the existing534 code-only holder; original595 metadata/source witness must be
archived first. No change to protected authentic fork/wire/proofs/control06.

Production delta against integrated main: three files103 added/69 deleted.
The 13.562/13.696 inter-request and98/99-second provider spans are not attributed
or claimed fixed by this change. Parent-owned AgentActivity itself remains the
original source; this does not invent a fence when that owner fails to return one.
