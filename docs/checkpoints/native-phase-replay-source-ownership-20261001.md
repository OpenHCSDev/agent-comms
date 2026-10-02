# Native phase and replay source closure inside #489

Receiving base: `55e6c36ad92a730f4b9ddddade934de238f8595c`.
Supplied implementation `36471530` was reread in full. This is a source implementation
checkpoint inside the existing lifecycle draft, not Ready or installed acceptance.
Source reasoning and implementation precede final batched validation.

## Existing owners and displaced decisions

`TurnPhase` already owns phase identity and behavior. The watchdog event now carries
the original member, and `stalled` returns that member. `ShutdownPhase` adds the
missing stopping behavior to this family: reject follow-up/cancellation and retain
shutdown across late progress. All original watchdog, backend and Pi command/event
producers consume the typed member. The watchdog observation does not update the
canonical registry lease or grant an input disposition.

`ReplayAssessments` already owns the durable execution-scoped replay facts and
monotonic SQL relation. Its `accumulate` and `revoke_for_retry` now own joins and
prior-proof revocation formerly decided in RecoveryMonitor and AttemptStore.
Both callers remain inside their original fenced transactions. Missing evidence
never grants safety; incoming UNKNOWN revokes it; original `record`, successor
constraints and SQL monotonic triggers retain settlement authority. Explicit
`observe_replay` records supplied evidence through the same owner, not a second
classifier. No schema, original disposition or retry behavior is changed.

Delete the watchdog event's retryable/replay-safe/possible-effect fields, its
independent classification, and every tool/output/compaction proxy writer. The
supplied `TurnExposure` and observation `TurnEffects` are not installed. The
existing `turn_effects.TurnEffects` ACP capability remains untouched.

The existing `PiEvent` family already decodes native retry events once.
`RetryAttemptEvent` is an abstract intermediate in that SAME family, factoring
three repeated external field declarations and observation algorithms. Existing
AutoRetryStart/SummarizationRetryAttemptStart/SummarizationRetryScheduled keep
their native names and concrete retry hook behavior. It adds no registry, wrapper,
policy or attempt store. `TurnState.attempt` carries the original Pi event, deleting
the copied tuple/dictionary and session current/maximum/elapsed fields. Optional
native counts remain an external-format contract, not internal lifecycle state.

Patterns: IMPL-12 (three algorithms), IDEN-3/5 (replay facts recoded as watchdog
booleans), BOUND-2 (original Pi retry data copied), TIME-9 (a typed wrapper over a
parallel policy would not repair ownership).

## Complete event, publication and persisted-reader search

[Core search](../../evidence/runtime-lifecycle-semantic-pass-20261001/phase-boundary/core-consumers.json)
records all authored Core Python hashes, raw producers, handlers, serializers,
persistence and replay consumers. These lexical candidates were resolved by reading
their owner methods; they do not by themselves prove dynamic call reachability.

1. `agent_events.TurnState` is an in-process watchdog observation. Producers are
   ProgressWatchdog.state and TurnSession.identity_uncertain. TurnProgress's generic
   consume path forwards AgentEvent to publication. AcpEventConsumer has no handler
   for this observation or generic handler which encodes it.
2. CommsAgent._emit_event dispatches AgentEvent through AcpEventConsumer. Existing
   registered handlers construct ACP SDK updates. RuntimeServer/SocketClient encode
   those SDK updates with `model_dump`, not the watchdog event. No transport decoder
   for `agent_events.TurnState` is declared or called.
3. `turn_lease.TurnState` is a DIFFERENT read-only projection of ActiveTurn. Threads,
   TurnRunner, TurnTranscriptUpdate and ACP TurnChangedUpdate consume this projection.
   Its original phase, identity, permissions and storage are unchanged here.
4. Terminal diagnostics encode original Done and measurements; request diagnostics
   encode lease/RequestProgress/process observations. Neither persists the watchdog
   event. A generic codec accepting dataclasses is not evidence of this event being
   written to a durable store.
5. [Pinned Toad source search](../../evidence/runtime-lifecycle-semantic-pass-20261001/phase-boundary/toad-consumers.txt)
   uses original object `37e7867bc646aac686837687c1ca9c775503c71b`: metadata decode,
   TurnChangedUpdate handler, ManagedTurnBinding and conversation invalidation use
   `turn_lease.TurnState`. No watchdog TurnState/replay-policy consumer is present.
   Conversation's separate `agent_events` import is resolved at its actual use;
   that import alone does not establish a watchdog-event reader.

Thus no persisted watchdog TurnState reader was found in this complete authored
Core plus pinned Toad scope. This does not claim unknown external consumers absent.
The supplied patch's proposed event wire migration is not implemented: no actual
watchdog serialization sink was found. The canonical lease ABI is unchanged.

## New-case reasoning and remaining integrated scope

A retry observation reuses the existing abstract PiEvent intermediate, with its
native declaration and behavior hook; no session copy, wrapper or watchdog policy
edit is needed. A phase is declared in TurnPhase; stall consumers receive the member.
Replay observation joins have one implementation on ReplayAssessments, not separate
caller calculations. Original transaction/fence owners still determine when a join
may be recorded.

This closes this semantic relation, not the whole #489 lifecycle. Budget/final
request admission, paused original writer/summary continuation, acquired reader
behavior across continued/recovery paths, Kepler's original source membership and
canonical terminal/readiness consumers remain in the same integrated draft.
No tests/native calls/provider calls/public mutation run during this source pass.
