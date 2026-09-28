# Original S2/S7 tracked native turn ownership

Read original S2-pi RPC boundary/turn state machine and S7 residual procedures
from plans/nominal_refactor/files.zip, plus round2/00-RULES. S2 assigns event
identity to PiEvent, command correlation to PiRpcChannel/PendingRequests and
per-turn state to TurnSession. S7 explicitly names run_native_pi_turn and
requires components owning their state rather than forwarding to shared locals.

TrackedTurnSession specializes existing TurnSession and uses existing MroDispatch
handler declarations for Response/InputCommitted/ContextCommitted/MessageUpdate/
MessageEnd/ToolExecutionStart/ToolExecutionEnd/AgentSettled. Deltas and assistant
messages dispatch through their existing nominal payload classes. One strict
receive loop, no new registry or event-name table. Correlation consumes the
original PiCommand through canonical PendingRequests exactly once. Latest native
context witness replaces the formerly accumulated context list.

NativePiRpcLaunch now owns tracked construction and private environment policy.
The two procedural entrypoints are deleted; production and test callers use the
class operations directly, with no aliases or re-exports. NativeContextProof and
its merged283 row decoder are untouched. NativeToolMode/OwnerToolSocket operations
remain the tool authority; Dalton owns their implementation.

Actual pinned native acceptance:9pass28.07s including2,098,094-byte RPC record,
mid-frame cancel/EOF with no replay, configured-provider reopen, provider failure,
and retained exact RPC rejection details. No paid provider call. Earlier failed
canonical Sol harness receipt stays failed; the subsequent real probe passed and
parent separately verified live155/156. This refactor changes no stored schema or
native package and has no live deployment by this sidecar.

Binding/tool refusals: initial shard124pass1skip and three stale pre-current source
fixtures failed before their intended assertions. Migrated those three fixtures
through existing manual_source/FieldCodec; all3 pass with unchanged assertions.
Remaining caller/selected-preflight checks and installed candidate acceptance are
being completed. Tests tied to moved implementation import locations use the new
owner; no old implementation import is restored.
