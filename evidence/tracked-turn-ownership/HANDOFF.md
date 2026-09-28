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

## Installed acceptance and remaining baseline debt

Noneditable wheel built from bd475eb7 installed under the owned
.artifacts/tracked-turn-installed. All9 actual native cases pass30.83s:
2,098,094-byte record, normal output, provider429/length, configured loading,
retained configured reopening, exact prompt refusal, cancel-large and EOF-large.
No live deployment, provider credit, schema or package change.

Caller check was explicitly interrupted to inspect failures (81pass10fail),
never reported as a green suite. Nine failures reproduce on unchanged
main451cc423 in an isolated source copy: seven obsolete ACP cursor expectations,
one old error message expectation, one incomplete foreground package fixture.
The tenth was our migrated maintenance patch target and is corrected.
Current typed reservation helper and all4 direct refusal/renamed-session cases
pass after fixing that helper's path reference. Separate selected preflight6pass.

Source accounting versus451cc423:536added/442deleted, net+94. The increase is
explicit state construction, declared event handlers and lifecycle methods;
there are no nested execution closures, new generic registry, retained old APIs
or compatibility alias. NativePiRpcLaunch.tracked is84lines and execute is62;
the complete run owns cleanup in50lines, with behavior on class handlers.
Tests at final caller patch:297added/219deleted (net+78), chiefly migrated caller
imports/patch targets plus the deletion guard and current source fixtures.
Production lint and undefined-name checks over every changed file pass.
