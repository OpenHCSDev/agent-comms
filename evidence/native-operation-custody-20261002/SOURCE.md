# Native operation custody

PR547, base732e8670; Mendel owns Core integration. The309 context consumer is coordinated separately. No public process, input, provider or installed package was changed.

## Implemented owner change

`NativeCustody.inspect_context` now observes an acquired child or refuses. Its base formerly called a preparation callback, which made the context request a native-launch authority. `BorrowedNative` still uses the active turn's original pending-response reader; `RetainedNative` still exchanges through its locked current child. Empty, reopen and retiring custody inherit refusal. `TurnRunner.inspect_context` no longer supplies preparation. `ContextRuntimeRequest` and CLI use the same request; no flag, catalog, fallback or alternative session is added. Explicit preparation, owned input and manual compaction retain their existing launch owners.

Production delta:8 lines deleted,6 added across native_custody.py and turn_runner.py. This fixes observation acquiring a writer; it is not measured attribution of the reported terminal tail.

## Terminal-tail source findings

Original418 spans8.450274s and15.819279s end after `TrackedTurnSession.complete` exits its original `AsyncExitStack`. `PrivateSendAdmission.execute` records that return before success verification and reply publication. Thus later publication cannot explain those particular spans. Model-request completion is not native `AgentSettled`.

Native960 `_runAgentPrompt` awaits agent completion and post-run handling, then emits `agent_settled`. Tracked input skips implicit compaction/retry; explicit queued inputs remain distinct. The managed global extension omits standalone activity/release hooks. These source facts do not establish elapsed time for historical native callbacks.

Tracked completion consumes native settlement, corroborates current input/context through its acquired evidence resource, finishes tool policy, and retires native/socket resources. Tool-round and terminal corroborations may concern different context generations; they cannot be replaced by a cached earlier proof. `PrivateEvidenceRead` verifies captured prefix bytes on every observation, retaining replacement/truncation fences.

`ChildProcess.stop` already joins one original retirement task. Failure and outer-close callers do not imply repeated physical retirement. Its group scans preserve birth/group fencing and exclude zombie/exited entries. `OwnerToolSocket.close` already cancels/drains handlers before listener completion. Ordinary `TurnSession` also has a distinct EOF-flush path for unretained protocols; this is not the tracked path's tail.

No source evidence yet attributes the historical interval to provider, SQLite, group scanning, tool handlers or proof IO. Merged544 owns AgentSettled/terminal-proof/retirement spans; parent337's next actual channel wave supplies them. No additional probe was sent.

## Source evidence and verification limit

`before-ast.json` uses existing NRA/refactor-audit `Package.load` on src/tests/tools:724 parsed modules, zero parse/enumeration omissions. It retains conservative nominal/member/literal candidates including inherited consumers. Python AST does not resolve dynamic receivers/MRO or native JavaScript; native960 and the actual extension hooks were read separately. Patterns IMPL-12/13 and BOUND-2 apply to the eliminated launch decision outside custody. Actual installed context verification remains pending; source publication is not live readiness.
