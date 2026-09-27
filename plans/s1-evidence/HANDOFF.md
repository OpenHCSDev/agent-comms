# S1 implementation handoff

Owner: Codex S1 worker, explicitly authorized by Tristan on 2026-09-27.
Worktree: `/home/ts/wt/comms-refactor-s1-events-20260927`
Branch: `codex/refactor-s1-events-20260927`
Base: `b1e5bfd` (`main`, including PR124). The implementation commit containing this file is the adoption point; the final publication receipt records its SHA and draft PR separately.

## Implemented slice

- A10: backend constructs frozen `AgentEvent` subclasses directly, including current thinking, tool-progress, compaction-progress, MCP-receipt, notice/error, and watchdog/retry observations absent from the older plan. Optional diagnostics are a dictionary, not the plan's provisional string. Tool diffs retain their original `ToolDiff` identity; opaque usage and tool argument payloads remain at their existing boundaries.
- A4: `MroDispatch` derives handlers from consumer method declarations and traverses event C3 MRO, with normal consumer method override semantics. There is no string event registry or codec. `ActivityEvent` declares activity hooks; `ToolStart` composes it with `ToolEvent`. Both consumers inherit shared activity and agent-info algorithms.
- ACP turn handling uses a per-turn `TurnEventConsumer` subclass, retaining orchestration closure ownership instead of introducing a parallel turn-state bag. `ParticipantEventConsumer` serves the registered agent-loop entry point. `AcpEventConsumer` projects typed observations directly into existing SDK updates; remaining `_emit_event` dictionaries are wire/transcript messages, not a backend compatibility parser.
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
- PR95 `b74774f` is not merged into this branch. Preserve its `_JsonLineReader.readline(max_bytes=...)` change, native launcher resolution, selected-summary modules, and compaction gate when integrating. Our reader is unchanged from this branch's baseline, and the manual bridge edit is only its final settlement block. PR95's `pi-native` / `pi-comms-native` gate is a separate hunk that must remain.
- Parent-owned `_private_cursor_metadata`, `_publish_private_cursor`, and transcript replay are unchanged. `protected-methods.json` records the baseline-identical source hashes. No history migration is included.
- External Pi JSON/RPC decoding remains S2. The remaining orchestration inside `_run_agent_turn`, including the placement of its closure-dependent consumer, remains a later S7 decomposition concern. The current typed producer/consumer protocol and shared algorithms are executable now.
- Prepared native-stack integration tests have their backend assertions migrated to nominal events. Unless explicitly recorded as executed in validation, collection/skips do not establish native-stack execution, installed-wheel behavior, or a live-runtime handshake.

No other worktree, live runtime, agent, UNKNOWN input, deployment, restart, merge, or model/provider call was changed or initiated. Test subprocesses used local fixtures. Disposable artifacts and locally installed test dependencies are removed before handoff; validation receipts remain here. No CI wait is part of this authorization.
