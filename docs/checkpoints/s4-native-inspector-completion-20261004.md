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
