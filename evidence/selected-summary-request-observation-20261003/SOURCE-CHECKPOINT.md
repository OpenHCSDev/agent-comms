# Compaction preparation and request observation

Production checkpoint: `bc10c16a7c71be8c03e5299625f9426aa9062a75`, PR555.
20 production lines deleted, 86 added across 11 files. Receipt additions do not
change those production bytes. This is qualified source, **not installed Ready**.

## Actual finding

Original469 boundaries committed its selected summary at +144.265s, then began
TRIAGE at +146.666s. TRIAGE/FULL model request spans total 7.305s. The historical
summary has no dispatch/headers/first-event clocks. Its preceding wait cannot
be divided into local preparation, authentication or provider time from the
available records. Original inputs, UNKNOWN and native/source proofs stay intact.
Original records are retained in `../scoped-owner-drain-20261003/original469-*.json`.

## Owner and callers

| Fact | Existing owner and complete related consumers |
|---|---|
| Request clock and first-delta consumption | `NativeRequestObservation`; normal `streamAssistantResponse` and common `completeSummarization` now consume its same iterator. The copied first-delta procedure is removed from the native-loop patch. |
| Summary source/plan/request policy | Original `compact` / `generateSummaryWithUsage` / branch summary all use common `completeSummarization`; prefix formation, concurrent map/synthesis leaves, budgets, retries, cancellation and terminal joins retain their existing owners. |
| Native clock publication | `AgentSession.agent.onRequestProgress` and existing `model_request_progress` event; `_summarizationRetryCallbacks` and selected `acExecuteSummary` forward that callback. No new event family or publication store. |
| Selected operation attribution | Original `SelectedSummarySlot` borrows the already-selected child and journal reservation, records through `PiEvent.observe_request`, and joins exact `SummaryOperationIdentity`, acquired `ProcessIdentity` and original `TurnLeaseFence` in the existing `.requests.jsonl` diagnostic. |
| Whole preparation elapsed time | Existing `NativeStartupAdmission.measurements` encloses `TrackedTurnSession`'s original `prepare_context` call. This includes source reading, native summary and required reopen rather than counting only the raw prompt. |

All three production `compact_selected` callers already own an active turn:
`SavedSelectedSession.prepare_context`, `maybe_compact_owner_turn`, and
`compact_manual_owner`. `owner.require_turn_lease()` captures that original
identity, not a new diagnostic admission decision. No late registry reread.

`PiEvent.observe_request` accepts a recording callback so the summary reader no
longer needs a fabricated `TurnSession`. Its boolean reports consumed capability,
not lifecycle state. A measurement cannot advance summary sequence, renew the
inactivity deadline, settle the journal or authorize input/replay. Original
progress/terminal events keep those decisions.

New optional values are diagnostic resources: the slot sink and optional summary
join in the existing diagnostic envelope. They do not represent missing turn,
input, source or operation state. Production selected summaries supply both.
Standalone SDK callers can omit the observation sink. A request's `sessionId`
is routing identity; its `inputId` can refer to a retained historical user or be
empty. Neither is inferred to be a newly admitted managed input. The selected
operation/lease/process join owns that attribution. Concurrent leaf durations
must not be summed as wall latency.

Catalog: IMPL-12 (shared request consumption), BOUND-2 (original event/resource
capability and operation identity). No new class, phase, status flag, registry,
cache, retry policy, journal codec or schema.

## Source and controls

AST before/after: existing refactor-audit `Package.load`, Python3.14, 724 Core
source/test/tool modules, zero omissions; 386→391 candidate references. Existing
Node26 bundled Acorn parsed 402 baseline JS modules and seven changed generated
modules in the final output (409 total), zero omissions, 178 final candidate
references. Callback/MRO candidates are source evidence, not dynamic-resolution
proof. TypeScript callback/iterator declarations were read separately, not
silently counted as parsed JS. See the four `*-ast.json` files.

Final checks each address a changed boundary:

- Exact stock patch sequence on 60 declaration/patch-selected files (1,363,636
  original bytes), with original hashes rechecked afterward. Seven generated
  modules parse. Prevents anchor, import-location and build-order breakage.
  Observer installation now follows prefix formation and accepts the original
  `onContextReady` implementation after context instrumentation.
- Common compiled summary function with unchanged donor imports: concurrent
  leaves have separate request IDs; throwing diagnostics do not alter result;
  an aborted result remains terminal with one call; first-delta is emitted once;
  early iterator return closes the original iterator. Controlled event streams,
  zero provider calls; **not native/ACP installed acceptance**.
- A generated helper event passes strict `PiRpcChannel` / `RequestProgress`
  decoding and original Python callback/diagnostic encoding; phase, prompt
  admission and liveness effects remain absent. Synthetic format-control
  identities are explicitly not historical summary receipts.
- `git diff --check` and changed Python/native source syntax passed.

The first module-control invocation used CJS export resolution and was rejected
by the dependency's ESM-only exports before any operation. Original negative log
is preserved. The corrected control uses Node's actual ESM resolution, without
altering production imports or package exports.

## Installation dependency and resource custody

Sch received exact source for normal reviewed native artifact preparation after
the independent556 publisher correction. Existing native960 and `pi-native.sha256`
are untouched. Full-tree/import pins and a fresh installed native selected-summary
clock path remain required; a projected module is not that artifact. No additional
public message/provider wave is launched here. Parent owns actual channel
validation once the reviewed pair is installed. No claim the 146s wait is fixed.

Owned disposable source projection:
`/home/ts/.cache/agent-scratch/mendel-summary-request-observation555-20261003`.
All controls are terminal; no native/UI/provider children were started. Receipts
and hashes are retained here before retiring the derived source projection.
Original stock, Native960, original469 records and existing holders remain protected.
