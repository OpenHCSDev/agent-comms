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

This is private instrumentation only. No SDK/artifact execution, provider input,
package changes, or study is authorized here. The original request-ID/JSON
observer corrections are retained; their actual SDK execution remains open.

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
pins source and raw controls. No actual SDK/inspector attachment, artifact loan,
provider input, or study was run. The changed observer's SDK qualification,
configured matched arms, capacity, and the full S4 study remain unfinished.
The original #659 timeout and tool-capture refusals remain unchanged. The
separate 30-pair/USD75 study is still unapproved.
