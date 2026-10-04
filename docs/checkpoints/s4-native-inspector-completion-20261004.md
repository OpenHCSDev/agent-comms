# S4 inspector completion

The original #659 SDK result completed, but Node waited for its debugger to
disconnect. The private controller joined Node before releasing that debugger;
the retained 45.033s timeout is unchanged.

This change belongs to the existing `observe_native_requests` resource and its
original WebSocket protocol reader. `ParentedProcess` and `ExitStack` own child
retirement. Node's `NodeRuntime.waitingForDisconnect` notification owns the
normal debugger release, rather than a stdout substring or a caller-specific
order of termination. Every pending protocol request must finish or reject when
the connection closes; setup failures must also release the connection.

Python source enumeration uses the existing refactor-audit `Package` parser
across `src`, `tests`, and `tools`. Native launch patching remains dynamically
bound and is not resolved by that lexical search. JavaScript ownership is read
at the original observer; Python AST output does not claim JavaScript coverage.

This is private instrumentation only. The initial source checkpoint did not
execute an SDK/artifact or provider. Parent subsequently renewed the original
086 read/execution loan for one changed authored SDK stream; that qualification
is recorded below. No package changes or provider study were authorized.

## Implemented

- The shared WebSocket reader subscribes to Node's own completion notification
  and disconnects then. Setup/refusal/disconnect also settle pending protocol
  requests and join active observations. Failures keep their original records.
- `observe_native_requests` borrows `ParentedProcess` through `ExitStack`,
  replacing its raw `Popen`/terminate/wait loop. Inspector stderr is retained.
  Configured workflow imports occur at the command boundary; importing the
  observation resource no longer requires loading the ACP workflow.
- The condition-application caller no longer closes observation custody early.
  Both existing native summary callers join their native child normally inside
  the observation scope, deleting their special disconnect-before-wait order.
- The authored checks exercise original observer code with protocol responses,
  including the existing request-ID selection and JSON expression. They do not
  import a native artifact or pretend to be SDK/provider acceptance.

Node declares `notifyWhenWaitingForDisconnect` and its completion event in its
[original inspector protocol](https://github.com/nodejs/node/blob/v24.13.0/src/inspector/node_protocol.pdl#L307-L323).
SDK stdout is neither an exit verdict nor the debugger's release authority.

## Qualification and remaining gap

Seven focused authored checks passed in 0.82s: normal runtime completion,
setup refusal, pending-response disconnect, paused-reader disconnect, owner
shutdown, malformed protocol data, and multi-child caller-failure cleanup.
The completion check also executes the original JSON projection expression on
authored tool callbacks and checks the exact request-ID breakpoint. Node syntax
and Python compilation passed. Initial collection's missing-ACP negative is
preserved; no dependency or environment was installed to fix it.

The existing Python AST parser found 736 modules before and 737 after, with no
parse omissions. Imports/calls cover the shared resource's configured command,
three-cut/application/construction consumers, and both native-summary consumers.
The controller's direct `Popen`, `terminate`, and `wait` decisions are removed;
the existing child owner carries retirement. This is lexical evidence, not a
claim to resolve dynamic launch patching or JavaScript with Python AST.

The published [receipt](../../evidence/s4-native-inspector-completion-20261004/receipt.json)
pins source and raw controls. The original #659 timeout and tool-capture refusals
remain unchanged. The separate 30-pair/USD75 study is still unapproved.

## Changed SDK boundary qualified

One original `native_turn_context_contract.mjs` controlled SDK stream ran with
the changed inspector against the explicitly renewed immutable 086 artifact.
The existing authored constructor/transform/converter/onContextReady path was
used to reach this changed boundary; no old history/selection controls were
repeated for additional credit. Both children exited normally with code 0 in
1.778237737s. Node's own completion event released the debugger; the controller
did not terminate it to obtain exit. Both original owned groups are retired.

The inspector selected exactly one committed-request capture and no constructor
preview refusal. The original reader authenticated all five segment lengths,
digests and JSON values: 12,214 UTF-8 bytes in total, including the 8,543-byte
system layer and 2,778-byte tool catalog. Original authored project/append
instructions survived, and the complete converter sequence joined to original
request ID `0a84628c-c2a9-474e-8bf2-e5735d708c64`. The whole-value reader remained
strict; SDK tool callbacks crossed original JSON serialization before inspection.

The authored journal was unchanged, hooks and canonical messages were restored,
and the original 086 complete package commitment passed before and after. The
renewed **086 READ/execution loan is handed back**, with both children gone.
The new source/terminal/reader/raw receipt hashes are retained alongside the
unchanged #659 negatives. The earlier seven authored checks were not repeated.

This qualifies the changed SDK observer lifetime/request-ID/whole JSON boundary:
one controlled stream, zero submitted prompts, zero provider calls, no enrolled
native claim or wire publication. It is not a configured provider turn, HTTP
capture, matched study arm, capacity, model recall or whole-turn speed result.
Those full S4 acceptance gaps remain open.
