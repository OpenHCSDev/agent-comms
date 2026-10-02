# Ready — original365 native progress publication (#528)

Production checkpoint: `92d02436`. Base `155b0007` includes523/524. Nine production files: **42 added / 35 deleted**. No native/provider artifact changes. Original S3 draft527/source24c606 remains parked and preserved.

## What changed

`RequestProgress` is an original native measurement (request/input/session IDs, clocks, transport and callback spans). It is not a turn lifecycle transition. Previously `ModelRequestProgress` embedded every changed timestamp in `ModelWaitPhase.source`; `SelectedParticipant` read and rewrote the shared registry before consuming the next native event. ACP `TurnProgress` repeated that interpretation/publication.

The existing `PiEvent`/`ModelRequestProgress` declarations now own request observation behavior. `TurnSession` supplies the acquired child identity to one scoped diagnostic callback; selected admission binds its original lease, and the ordinary ACP OwnedTurn supplies its existing TurnProgress observer. Every sample reaches the SAME existing requests.jsonl resource. There is no new queue, timer, cache, registry, clock, source mirror or type catalog.

Removed the ModelWaitPhase.source field, its phase/diagnostic projections, both consumers' request-as-phase loops, and the repeated model_request decision. Native model/tool/compaction/cancellation/publication phases and their controls remain with the existing lifecycle owners. Transport microstage details remain in the original diagnostics instead of the global registry phase; the public model phase reads Waiting for model.

Pattern relation: IDEN-1 (one field answered lifecycle and request measurement), IDEN-3 (absence as phase meaning), IMPL-12 (two consumer interpretations). Existing method hooks and declaration-owned event behavior close the family; no kind-change filter. `request_observer | None` denotes an optional diagnostic callback resource for standalone backend calls, not domain readiness/state. Managed selected and ACP paths always bind their original observer.

## Actual original365 cause and limits

`original365-timeline.json` joins original assignments → NativeRuntimeInput.stage/input_id → exact native journal input/assistant parent IDs → original request diagnostics → reply wire times. Source root remains read-only.

| Owner | Native triage input→answer | Full input→answer | Answer→wire |
|---|---:|---:|---:|
| helper | 11.679→14.829s | 36.809→43.074s | 8.170s |
| pr159 | 11.193→14.930s | 35.311→41.684s | 11.249s |
| helper2 | 15.033→18.370s | 41.819→47.091s | 11.237s |
| boundaries | 14.702→19.710s | 41.965→47.348s | 15.932s |
| compaction501 | 16.174→19.276s | 44.625→49.085s | 14.827s |
| compaction499 | 15.846→19.579s | 43.722→49.731s | 16.370s |

Native finished sample receipt lag: triage4.581–16.303s, full2.593–9.300s. Actual native callback totals3.429–13.957ms. Fourteen request measurements per input meant168 measurement-driven phase publications across the six original triage/full exchanges. Their awaited global publication lies directly in the read loop. Historical per-write CPU/lock profiling is unavailable: do not attribute every second to one lock or claim provider queue latency. Initial preparation/startup and later response publication/child retirement remain independent measured work; this source checkpoint does not claim all remaining latency fixed. Original six replies completed66.102s and peer closure109.348s after524.

## Changed-path validation

- Normal wheel/dependency installation, SDK0.12.1, unchanged full-trust Native5184; pip check passes. **339 installed production source/resource files equal92d02436**, direct URL/wheel proof retained. Prefix `.artifacts/runtime-native-progress528`; total allocated30,461,952 bytes (dependency hardlinks included), wheel843,776 bytes. No source PYTHONPATH/overlay/global install.
- Existing actual native/ACP parallel-channel journey: **PASS20.41s**. Three actual owners; original canonical stages/IDs chosen by the endpoint, overlapping triage requests, two exactly-once replies, all six original/peer claims settled, seven actual native requests. **All105 original timing samples retained across seven actual input IDs**, native child identity and original lease joined. Maximum parent receipt lag **0.073112s**. This localhost endpoint is a source-path control, not configured-provider/live latency acceptance.
- Existing native phase control: **PASS0.07s**, detects loss of actual tool-start/end phase/control transitions and confirms unknown event does not manufacture phase.
- NRA AST before/after mapping across tracked Core src/tests/tools: no parse failures. Mapping is source evidence, not dynamic resolution proof. Native JS producer is unchanged/inspected, not parsed by Python AST; Toad status consumers use existing phase.summary, not RequestProgress/source.
- Exact seven child process birth identities and three owner identities verified absent after cleanup; serving thread retired. Existing evidence hook retained full private original bus/native sessions/proofs before fixture disposal. No public writes/stops, uncertain replay, new paid calls or provider budget.
- First pytest invocation rejected repository-wide optional plugin flags BEFORE running any test/provider/owner; corrected addopts to run only this one changed-path gate. Original failed invocation retained.

Original public request records and sanitized join: `/home/ts/.cache/agent-scratch/comms-cross-owner524-20261002/live365`. Changed-path original fixture: `/home/ts/.cache/agent-scratch/comms-native-progress528-20261002/installed-parallel02/private-originals`. Both preserved.

## Installation / next step

No SQL/native/source schema changes or carry. ModelWait.source removal affects runtime registry phase shape only; quiet all-owner idle publication has no active phase to carry. Read-only current registry/release receipt inventory showed no ModelWait/source members; future preflight must use its original source interpreter and require idle rather than adding a legacy decoder. Durable wire/native history/UNKNOWN/input/goal facts are unchanged.

Parent reviews/merges/pairs the source checkpoint, then performs the authorized actual configured #openhcs all-capable reply+peerACK journey. No repeated unchanged local gate or new public probe from Mendel.
