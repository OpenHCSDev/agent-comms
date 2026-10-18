# P488: native argument ownership

## Source relationships

The existing refactor-audit overlay parsed Core production sources before editing (`p488/before-ast.json`, plain-text output; stderr empty). Source reads and consumer searches covered NativeExecutableTool, CodingCall, OwnerToolSocket, NativeToolCall, CallClaim, CallObservation, ToolCallContent, ToolExecutionStart, ToolStart, ToolDeclaration, NativeArguments, ThinkingLevel and SelectedSession. Installed Pi bash.js, validation.js, agent-loop.js and channel_coding_tools.mjs established the external format and request ordering. Dynamic callback/MRO and factory resolution were checked by reading their actual implementations; the AST overlay does not prove those relationships or runtime behavior.

| Fact | Owner after repair | Consumers |
|---|---|---|
| Whether arguments form an executable tool | NativeExecutableTool's existing field codec | CodingCall decoding and owner permission check |
| Why a declared call is refused | ArgumentRefusal, a CallClaim state | Existing socket failure response and native error terminal |
| Call identity and observed start/error | Existing broker call and CallObservation | Native round, start/end events and socket requests |
| Whether refusal may replace an unresolved request | AwaitingAnnouncement / AwaitingClaim | RefusedCodingCall matching a normalized early request |
| Which thinking level CLI text names | ThinkingArgument.from_text through ThinkingLevelValue | NativeArguments.thinking, SessionLifecycle and native launch selection |
| Whether thinking produces an option | ThinkingLevel / NoThinkingOverride | NativeArguments.with_thinking |
| Native option spelling | Existing option declaration | ThinkingArgument.argv and SelectedSession through NativeArguments |

The call object is not replaced when Pi's normalized socket request arrives before its original malformed call. Its claim becomes a refusal; it never gains execution permission. A changed already-declared valid payload still fails strict matching. Existing uncertain claims are not reopened. Primitive argument values survive protocol decoding so Pi can return its real validation error; they are not mistaken for a broken native turn.

Deleted competing paths: four optional-field non-null constraints, the manual argument-object check, escaping argument decode errors, raw thinking strings in NativeArguments readers, and ThinkingLevel's separate launch-arguments forwarder. The CLI argument factory parameter removes the runtime import cycle rather than adding a function-local import.

## External contract

Pi's bash schema declares timeout as an optional number. Its installed validateToolArguments calls normalizeOptionalNulls before validation, deleting optional null fields. A direct SDK validation returned only the command for timeout:null. Core now uses the same omission semantics through its existing field declarations. Unknown thinking names fail during NativeArguments.parse; absence is NoThinkingOverride.

## Working behavior

89 focused tests passed. The actual native saved-thread process, ACP input owner, authenticated socket, normal Pi bash executor and saved SDK history were exercised with controlled local provider replies. One fresh user input produced:

1. `P488_NULL_RAN` with timeout:null.
2. Pi's real validation error for primitive arguments `"pwd"`.
3. Core's refused result for a boolean command field.
4. `P488_NEXT_RAN` from the next actual bash call.
5. Final answer `P488_DONE` saved in history.

Five controlled provider posts, no retry or original-input replay; original saved history remained a prefix. Owner shutdown closed its resources. One older ACP idle-preparation test was excluded for the separate P437 preparation-root defect. This is native application-path verification with branch Python and the installed SDK, not a claim that PR1480 is already installed.

## Source growth

The repair removes 54 and adds 144 production lines, net +90 before this documentation-only commit. A Src-Budget of 100 covers the real added behavior: the explicit refused claim and unresolved-call reconciliation, shared valued-option parsing with a typed thinking record, and propagation of malformed protocol values to the existing tool-result path. It does not permit new lint, type or structural debt. No checker or suppression was changed.
