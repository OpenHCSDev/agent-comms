# Retire competing turn authority in original fixtures

Mendel owns the complete six-module fixture family: test_turn_runner, test_acp,
test_acp_private_nk_delivery, test_acp_input_disposition,
test_runtime_goal_retry_running and test_stack_retry_running. Existing
NativeBackendFixture saved SDK source, canonical_agent, OwnedTurn and registry
TurnState/owns_turn carry preparation, admission, busy state and retirement.
The removed active_turns map is not restored. Read/write consumers and related
fabricated input/turn authority are migrated together; cancelled, isolated,
model/compaction/retry/UNKNOWN cases and resource cleanup remain meaningful.

TIME-1/TIME-6, BOUND-2, IDEN-5: stale test consumers bypass current owners.
Original AST/declaration reading precedes changes. No global session_dir rename,
compatibility reader, fake witness, new fixture framework or production map.
Current main is the source base; F4 evidence and source stay on their published
branches. Open627/632/432 do not claim these six test files or native_backend_fixture.
Shared fixture helper changes coordinate before editing.

Source-only now: no package write, runtime launch, environment/worktree, native
build/copy, provider or App. Final bounded affected real-owner gate follows the
coherent migration only on separately cleared/granted existing540. Current
critical resource pressure is not overridden. Original private histories,
input proofs/UNKNOWN and accepted prior checkpoints remain protected.
