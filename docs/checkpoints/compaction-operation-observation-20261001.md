# Compaction operation state and original leaf observations

Owner Arendt. Source base main `9954cdd7`; separate from unfinished #489
Native6 custody. Schrodinger owns the original CompactionPlan producer;
Heisenberg owns actual Toad rendering. Parent owns activation. This draft is
not Ready. Source reasoning and coherent declaration change precede validation.

## Required relation and existing owners

The native `compact` producer creates one `contextPolicy.plan` from history
and turn-prefix sources. Every concurrent leaf calls that same plan's
`progress(leafPhase)`. Source byte counters and start time belong to the whole
operation; summaryPhase identifies the emitting leaf. Map/synthesis can also
overlap. The leaf observation is not a new whole-turn lifecycle phase.

The existing `CompactionSourceProgress` declaration is the shared native/ACP
value. `CompactionPhase` receives it and derives percentage/start time. The
existing equality declaration already excludes observedAtMs from semantic
state comparison. The same original boundary must classify summaryPhase as a
leaf observation without throwing away the field or creating another value,
state, registry, projection, timer or delivery path.

## Complete determining producer and consumer census

| Question / boundary | Original owner and consumers |
|---|---|
| Aggregate measured source work and start | Native CompactionPlan owns sourceBytesDone/sourceBytesTotal/startedAtMs across all leaves; no Python estimate or counter. |
| Emitting leaf and observation clock | CompactionSourceProgress carries original summaryPhase/observedAtMs. These are observations, not changes of whole-turn state. |
| Native event decode | Pi CompactionProgress and AgentCommsCompactionProgress decode the shared value through original FieldCodec; original wire fields remain unchanged. |
| Live selected/native delivery | SelectedPiSummaryRpc and PiEvent.apply emit original CompactionSummaryProgress. Manual and adaptive callers use the same ACP event owner; no event suppression or batching timer. |
| Turn state | CompactionObservation → TurnRunner.transition_turn uses the existing CompactionPhase equality; actual operation/counter changes remain semantic changes. Cancellation/end and exact operation ID fences remain unchanged. |
| Original registry publication | AgentActivity.transition_turn → Registration.transition_turn → RegistryDocument.transition_turn uses the exact original lease and one document. No observer state or last-phase copy. |
| ACP publication | AcpEventConsumer.on_compaction always publishes the original CompactionChangedUpdate after observing the turn. It retains every raw leaf observation/text delta. |
| UI | Installed Toad CompactionRenderer.selected_summary_progress uses the original event's leaf map filter. Canonical turn label/start use aggregate counts/time. This source change neither removes that leaf field nor changes its renderer. |

Search scope: all production Core modules and the installed Toad at
`toad-receiving-reader493-scroll275-20261001/.artifacts/runtime-evidence498-20261001`.
The only production direct summary_phase reader in Toad is its original map
filter. Python lifecycle reads source.label, source.started_at_ms and equality;
it does not interpret leaf phase as a control phase. Source searches are a
consumer census, not proof of observed latency or installed acceptance.

## Actual retained trace, scoped strength

Schrodinger's readonly original 95fe attempt trace has 5,105 progress events,
5,100 text deltas and 21,406 characters; native observation medians 23ms/33ms
for the concurrent leaves. It reports 2,386 history/current-turn alternations
and 2,412 turn_changed records. This establishes publication amplification,
not the amount of elapsed delay attributable to the callback or provider.
The original attempt has since committed; it must not be replayed.

Patterns: IDEN-1/3, IMPL-5, AGENT-6. Existing owner first; declaration equality
closes the required relation across every consumer. No new classes or copied
decisions. Wire/store/native formats unchanged. Final batched sanity and an
affected installed original native/ACP/UI journey come last; no passing or
Ready claim has been made yet.

## Implemented declaration checkpoint

One production declaration changes: summary_phase now has compare=False in
the existing CompactionSourceProgress, as observed_at_ms already does. The
full original wire field remains encoded/decoded. Aggregate counters/start
still distinguish source work; CompactionPhase's operation ID and lifecycle
remain exact. No type, policy, timer, cache, observer inventory or ABI is added.
Production delta: one deleted line / three added, including the source rationale.

After completing source reasoning and implementation, one final focused batch
ran existing progress/codec/phase controls, extending its unchanged-work case
over all concurrent leaf observations. Eight passed in 0.11s. These controls
check raw leaf text/phase survive FieldCodec and ACP update encoding while
unchanged whole-operation work does not become a new turn state. They are not
an installed configured native/UI qualification. That remains unfilled and
must join the existing receiving configured compaction journey, without
replaying the completed original 95fe or creating a competing provider fixture.
