# S1 implementation handoff

Owner: Codex S1 worker, explicitly authorized by Tristan on 2026-09-27.
Worktree: `/home/ts/wt/comms-refactor-s1-events-20260927`
Branch: `codex/refactor-s1-events-20260927`
Current integration: main `e4cd10e` (PR126) merged as `acd64af`, then published PR95 `60e6cc4ceea7b8e4a2c3a33b47fb40a5765a446a` merged as `7914c9f`. Original S1 implementation: `b547552f38077618dc93aec4707dc1e43bb122b9`. Draft PR: https://github.com/OpenHCSDev/agent-comms/pull/127. Current implementation/adoption commit: `48fd974fe52f3b2205f916ee4fcf792397c33c67`. `publication.json` records the exact source handoff; a following receipt-only commit does not change executable source.

Latest owner directive acknowledged: complete S1 across the integrated source, with A8 and parent live ownership preserved. No dirty PR95 lifecycle work was copied. S1 is complete on the published integration baseline; the remaining boundaries below belong to other surfaces or validation environments. See `integrated-validation.md` for the resumed execution evidence.

## Completed S1 surface

- A10: backend constructs frozen `AgentEvent` subclasses directly, including current thinking, tool-progress, compaction-progress, MCP-receipt, notice/error, and watchdog/retry observations absent from the older plan. Optional diagnostics are a dictionary, not the plan's provisional string. Tool diffs retain their original `ToolDiff` identity; opaque usage and tool argument payloads remain at their existing boundaries.
- A4: `MroDispatch` derives handlers from consumer method declarations and traverses event C3 MRO, with normal consumer method override semantics. There is no string event registry or codec. `ActivityEvent` declares activity hooks; `ToolStart` composes it with `ToolEvent`. Both consumers inherit shared activity and agent-info algorithms.
- ACP turn handling uses a per-turn `TurnEventConsumer` subclass, retaining orchestration closure ownership instead of introducing a parallel turn-state bag. `ParticipantEventConsumer` serves the registered agent-loop entry point. `AcpEventConsumer` projects typed observations directly into existing SDK updates; remaining `_emit_event` dictionaries are wire/transcript messages, not a backend compatibility parser.
- Manual compaction now emits the same nominal family and uses the shared ACP presenter. `ManualCompactionEnd` owns its safe abort explanation; no second compaction formatter remains.
- A6: one `PendingRequests` map correlates setting results by nominal result class plus request ID, handles failures and completed/cancelled futures, and retains caller-owned cleanup. This is the S1 setting-result implementation; S2 may extend the same owner for its RPC result contract.
- Stream settlement and subscriber settlement are distinct classes. `NoActiveTurn` replaces the internal empty-ID sentinel; the historical ACP replay value is produced only at publication.
- `CommsAgent.finish_turn_stream` retains the early native stream fence. `settle_turn` centralizes terminal publication and waiter release for relay, agent, and manual-compaction completion. Waiters are released after final output even when native stream settlement happened earlier. The bridge now releases waiters, including when client publication fails.

## Decisions and semantic evidence

Read the supplied index, shared-abstraction plan, S1 receipt, NRA skill/API/architecture documentation, and OOPSLA `03_model_oopsla.tex` / `04_separation_oopsla.tex` in `papers/docs/papers/paper1_typing_discipline/latex_jsait/content`.

The relevant formal distinction is identity relative to an observation profile: `Chunk` and `CommittedProgress` have the same text-shaped profile but require different behavior. Class identity supplies the distinction; consumer payload probing cannot reconstruct it. Shared activity behavior factors through the declared `ActivityEvent` capability. Its diamond MRO and constructor/frozen-field behavior are tested. A test-only `ContextWarning` subclass reaches both real consumers with only its declaration and a production site; no consumer edits or name registration.

OPEN-1: goal synchronization is ACP policy, not intrinsic backend lifecycle membership. The participant has no corresponding lifecycle reaction. ACP therefore declares handlers on the relevant nominal classes, preserving tool synchronization after publication and before sent-tool-message publication. No invented lifecycle capability is imposed on every consumer.

OPEN-2: manual compaction claims a real turn, so its terminal fence participates in ordinary waiter release. Tests prove publication precedes release and that the fence is released on publication failure. The current operation is `release_waits_after_terminal_turn`, not the old plan's `pause_waits_after_terminal_turn`.

OPEN-3: the registered `agent-comms-agent` entry point remains supported. OPEN-4 fields are captured from current producers in `event-declarations.json`. OPEN-5 tool-name routing remains deferred to the goal-tool surface.

## NRA coverage and proof limits

NRA checkout: `/home/ts/code/projects/nominal-refactor-advisor`, HEAD `52fe8b4666a20583f0ddf8ed3b7a9e89857e4809`. CLI invoked via its `.venv/bin/python` with explicit `PYTHONPATH`, single parse/analysis workers, and worktree-owned caches.

Before/after scans use the complete `src/agent_comms` context and focus reports on S1 files (including new modules after implementation): `exact_compact_global`, 79 detectors, zero omitted, zero findings. JSON receipts are adjacent. The initial full-payload attempt exceeded a 165-second bound; exact compact global scanning then completed successfully. A later cached rerun reported only 43 local detectors; it was rejected as global evidence and replaced by an uncached final scan. Zero findings is not an equivalence proof and the original untyped dispatch was already a known detector blind spot.

These are authored producer/handler moves. The inspected `DispatchToPolymorphismOperation.required_source` accepts synchronous top-level `ast.FunctionDef` targets, rejects async functions and methods, and cannot synthesize this async closure migration. No claim is made that NRA proved authored bodies equivalent or executed a native-equivalence proof. Behavioral evidence comes from the focused tests and real subprocess/package paths in `validation.md`.

## Integration boundaries

- No foundation/S6 cherry-pick is necessary: the events are not serialized and dispatch keys are classes. No A1/A2 implementation, duplicate codec, registry, or dependency was added. Foundation readiness commit `19323fb3ff421fc94b81f419932d71f57ad01523` was inspected when published.
- Published PR95 `60e6cc4` is fully integrated. Its two newly added strict-reopen failure producers and all new backend-consuming tests use nominal events. Optional `_JsonLineReader.readline(max_bytes=...)`, native launcher resolution, and manual bridge canonical-writer gate remain intact. ACP selected-summary compaction runs **after** passive-awareness prompt augmentation and passes `on_admission` supplying `SelectedSummaryAdmission`. No selected-summary lifecycle/compaction module was independently edited.
- Parent-owned `_private_cursor_metadata`, `_publish_private_cursor`, and transcript replay are unchanged. `integrated-protected-methods.json` records equality with published PR95, including its bounded reader. No history migration is included.
- External Pi JSON/RPC decoding remains S2. The remaining orchestration inside `_run_agent_turn`, including the placement of its closure-dependent consumer, remains a later S7 decomposition concern. The current typed producer/consumer protocol and shared algorithms are executable now.
- Prepared native-stack execution is now established locally: nine compaction cases plus 17 settlement/inbox/interruption cases pass; four mounted Toad UI variants remain unexecuted. Two old external-TypeScript test fixtures now use a test-only SDK/RPC host of the verified package with inline reactions and Python-catalog tools. The production immutable import boundary is unchanged. These tests do not establish installed-wheel or live-runtime readiness.

No other worktree, live runtime, agent, UNKNOWN input, deployment, restart, merge to main, or external model/provider call was changed or initiated. Test subprocesses used local fixtures. Disposable artifacts and locally installed test dependencies are removed before handoff; validation receipts remain here. No CI wait is part of this authorization.

## Adoption and exact next boundary

Use the branch head recorded in `publication.json`; it contains normal merges of main and
published PR95. PR127 targets main, so its overall diff includes PR95 until that branch
lands. Review S1 alone with `git diff 60e6cc4..HEAD -- src tests`.
Merge this branch normally into the adopter's isolated worktree, retaining any newer
parent native_pi/coordinated_runtime/diagnostics changes. Do not replace those files or
whole ACP methods from an old snapshot. At ACP conflicts preserve the final prompt
ordering, one-use admission callback, parent cursor methods, and nominal consumer.

PR95's independently owned follow-up lifecycle work is not included: linked selected
attempt retirement, ordinary-input admission after a linked result, correction handling,
and default activation remain with that worker. Integrate its **published** follow-up
normally; retain the typed test fixtures here. No idle wait or CI gate is required by
this S1 handoff. S2 external RPC decoding and S7 orchestration decomposition remain
separate surfaces; A8 LockedStore and goal persistence are not part of this branch.

Recreate the own native bundle with `TMPDIR=$PWD/.s1-artifacts/tmp stack/bin/prepare-pi-native`
after creating that directory. Disposable native bundle, test roots, and npm dependencies
are removed after validation. Commands and failed-to-passing fixture evidence are retained.
