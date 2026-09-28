# Original refactor completion audit — 2026-09-28

**Audited parent229: `e6d7fe085b318e672c1b97f08df53ea382b92d1c`.**
Production inspection is at its unchanged source predecessor `c3e7252a`;
latest parent adds255 caller fixes and final-deletion/data receipts.
Read-only inspection of committed source, original plans/audits, actual GitHub PRs
and existing receipts. No implementation, tests, provider, live-state or activation
work. Only this report/evidence was written in S13's own tree.

**Result:** original major owners and their named obsolete implementations are
accounted for. Parent advanced during inspection: manual-writer deletion,
`broadcast` alias deletion and PR255/256/257 are now integrated; these are merged
into229, not main. Do not reassign those implementations. Whole completion still
needs the concrete package/caller/installation closures below. Historical blanket
guards and exhaustive experiments must remain explicitly qualified.

## Concrete remaining closures, in priority order

| Requirement / gap | Verified evidence | Existing owner / completion action |
| --- | --- | --- |
| S3/S5/R7/PF1 actual selected claimed-write package | Parent `selected_tool_broker.py:151–162` requires extension digest `361bce47…`; staged immutable0d7 package contains `722dcd9f…`. Independently compared the actual file to the current guard; mismatch receipt included. PR255's handoff reports two actual loader refusals at this boundary. | **Parent package/integration**: prepare a NEW complete matched bundle and current manifest. Run the affected selected-tool launch/write acceptance through that package. Keep guard and existing immutable bundle intact. Fresh/copied get_state success does not prove selected-tool launch. No repeat302/604MB proof needed unless its boundary changes. |
| PR257 package-side deletion | Source inventory capability field and historical fixture copies are deleted in parent;0d7 still contains the previous compiled package. Toad118 is merged into107, whose latest body explicitly separates source deletion from rebuilt-package activation. | **Parent + Copernicus107**: fold257 into the same new package, pin current core/Toad, finish changed installed MCP/settings/selected-tool checks. Source merge alone does not remove the deployed field. |
| D1/S7 old manual test callers | `tests/test_acp.py:196,239,316` patch deleted `manual_compaction_bridge.manual_compaction.ManualCompaction.run`; `test_acp_compact_command.py:187` patches deleted module. Still present in255 `1a5c2522`, after it merged current parent. | **Nietzsche (255 followthrough)/parent**: migrate surviving ACP terminal/refusal/lifecycle assertions to canonical `compact_manual_owner`; delete only obsolete writer-specific assertions. Do not restore ManualCompaction. |
| S4/R2 alias test/cutover closure | Production alias and APIs are deleted. `test_end_to_end.py:177` still sends to unregistered `broadcast`; `test_owner_compaction_commit.py:585` does likewise. `test_historical_page_scope.py:46,66–67,98` still creates/expects old global alias rows. | **Nietzsche (255 followthrough)/parent**: current callers use `#all`; keep the real-thread-named-broadcast DM test. Delete obsolete runtime alias-reader acceptance. Parent's corrected `global-channel-data-audit.json` checks serialized `to`:146 current +8,420 archived rows contain no broadcast alias targets, so no retained-data alias conversion is needed. |
| D22 history preservation + executed-tool deletion | Eight files remain under `tools/cutover`; source converter reads `Message.from_wire` and requires unchanged encoded public payload (`wire_history.py:64–75`). Latest `d22-install-final-deletions.log` passes copied-root atomic install/reopen:145 current/8,420 archived rows,104 threads,13UNKNOWN,301-file backup and read/session/diagnostic preservation. Corrected actual-data alias audit reports no conversion needed. | **Parent**: retain identity/order/body/UNKNOWN/barriers, complete quiet conversion and affected installed acceptance; then remove executed one-shot converters and exclusive tests. Existing source backup/history remains preserved. No permanent runtime alias. |
| R0 packaged guard | Core254 is still unique source work (not ancestor of parent); Toad117 is its paired test-collection consumer. | **Pascal254/117 + parent**: integrate the published shared ratchet and delete superseded script/roster; run local affected checks. This is not a CI hold. |

No new implementation surface is assigned by this audit. The package mismatch was
already reported by255; it is confirmed here, not independently implemented.

## Requirement → current owner/deletion evidence

“Accounted for” below means the named source/caller replacement is present and
its predecessor is deleted, using existing behavior receipts. It does not mean
every historical acceptance experiment was executed at this exact parent head.

| Original requirement | Current trace / existing evidence | Remaining qualification |
| --- | --- | --- |
| C0: owned composition, remove shared aggregates | `comms.py` construction root; direct domain modules. `operations.py`, `declarations.py`, imports/mixins absent; c0 integration/live receipts. | Current combined installation is parent-owned. No new C0 implementation needed. |
| S1/A10: typed events, MRO reactions, one settlement | `AgentEvent`, `AcpEventConsumer`, participant consumer and `TurnRunner.settle_turn`; event/runner extension and terminal-order tests; dict-event shim absent. | Manual test callers above; original exhaustive recorded-stream equivalence remains unproven. |
| S2/R1: decode Pi once, commands/correlation, phase/failure/usage | `PiRpcChannel`, `PiEvent/PiPayload`, `PiCommand/PendingRequests`, `TurnSession`, phase/failure/account owners. Old line reader/stream procedure and Mapping event adapters absent; S2/R1/native receipts. | Current selected extension package mismatch; literal size/exhaustive replay qualifications below. |
| S3/R7: nominal coordination and one selected execution | Declared assignment/execution/attempt/obligation/recovery families, typed gateway; `SelectedExecution` owns transaction and exact lease. Old `WakeClaim`, `ClaimState`, `ProjectionRecord`, `run_one_sealed_claim` absent. | Preserve current selected native acceptance while repairing package pairing; no duplicate executor. |
| S4/R2: exact reads, route/catalog ownership | `ReadLedger`/display basis, `ResponsePolicy`, `CatalogDocument`, `MessageWireCodec`; old scalar marker/forwarder/catalog loaders and broadcast alias removed. Existing read/rebind/source-history receipts. | Alias test/retained-data disposition above; paired107 installation. |
| S5: distinct identity and actual claims | `ThreadIncarnation`, `OwnerIdentity`, `TurnIdentity`, exact `TurnLeaseFence`, typed FileClaimPath consumers. `resource_claims.py`, raw normalization adapters, epoch identifiers and old claim/lease aliases absent. | Package pairing; D22 retains old durable facts through one-shot conversion. |
| S6: declaration-owned export scope/limit/format | Actual families/current CLI and Toad callers; kind enums/metaclass/factories absent; export golden/new-case and installed cleanup receipts. | No new source deletion gap found. |
| S7/R3: input-attempt ownership | `InputDispositions(LockedStore[InputDocument])`, typed attempts/current runtime inputs; old ACP/public-drain/cursor route retired. |255 still closes surviving fixtures; UNKNOWN is not a reconstructed queue permit. |
| S7/R5: runtime/collaboration documents | `RuntimeInfoStore` and `SharedLedger` use LockedStore; activity keeps its real append/checkpoint owners. R5 installed/metadata receipts retained. | No reason to merge distinct stores or delete real transaction fences. |
| S7/R6: native transcript boundary | `NativeEntry`, typed transcript/replay/page/routes; obsolete bag/parser/bridge aliases gone.243/244 integrated indexed storage/streamed proof; current302/604MB CLI→prepare→commit→reopen receipts in232. | Quiet final package activation; no repeated capacity run for this audit. |
| S7 residual component ownership | SessionLifecycle/InputDrain/TurnRunner/ConfigOptions, WireLog/Publisher, Registration; old ACP façade, public writer and supervised_cutover removed.256 removes orphan awareness ledger and callers. |257 deployment,255 callers and parent one-shot deletion remain. |
| S8/R4: goal lifecycle/generation/attempts | GoalState/GoalAction/GoalExecution/GoalChanged, GenerationState and typed goal tables; raw constructors/state rosters, automatic grant adoption and old goal loaders gone. Existing goal/Retry/UNKNOWN receipts. | Current goal fixtures in255; no new goal mechanism. |
| D1: manual lifetime/correlation/cleanup | Actual bridge calls `compact_manual_owner`; `manual_compaction.py` and manual_preflight helper now deleted. Canonical manual/native receipts retained. | Four stale mixed-test patch sites listed above. |
| D2: journal states | `OperationState`, `SummaryState`, `PublicationState`; stores/commit/admission consume them. Compaction D22 preserves uncertain records/barriers. | Parent conversion/activation; never reconstruct returned authority from a visible row. |
| D3: source witness | `NativeWitness` and FieldCodec request/results on current PiHelper path; repeated witness bags/preparation maps retired. | Existing current-receiver proof retained; package selected-tool seam is separate. |
| D4: remove detached summarizer/string adapter | Selected summary transport/current result owners; `summarize_native`, `_SUMMARIZE`, `PreparedOwnerSummary` and old outcome adapter retired. | No additional provider path required. |
| PF1: per-tool-call behavior | `NativeToolCall`/observation/admission states shared by normal/selected socket policies; parallel lifecycle containers retired. | Selected compiled extension must match this broker. |
| PF2: metadata/seal declaration owners | WireMetadata, PrefixWitness and pending/final seal owners shared across append/checkpoint/registry; old schema mirrors removed. | Parent D22/certificate conversion and installed receipt. |
| PF3: native proof authority | `NativeContextProof.read_evidence` uses NativeEntry; five callers migrated. Old `_read_native_context_evidence`/`load_native_context_proof` absent; B3/B4 native/claim receipts. | No duplicate validator or raw claim adapter needed. |
| PF4: route observation | Existing validated route boundary replaces observation-time Comms construction; core214/Toad103 receipts. | Parent paired107 current route/installed checks. |
| PF5: captured historical scope/index | `MessageBus.historical_page` uses captured scope and `scope.index_targets`; per-message scope/callback reconstruction removed; sparse/history/pagination receipts. | Preserve archived semantics while disposing historical broadcast fixtures/rows. |

## Literal acceptance discrepancies — keep explicit

- Re-ran the existing **read-only AST/token inspection**, not tests or an NRA
  scan: zero original retired identifiers, aggregate modules/imports, internal
  epoch identifiers or old lease/assignment local names. Source receipt attached.
- The old universal S7 size rule is still false: **8 modules >1000 lines;
  36 functions >100 lines**. Original S2 also demanded <=100-line methods;
  backend `stream_agent_events` is106 lines. These locate a discrepancy; they do
  not establish another missing behavior owner. Parent must record disposition
  explicitly rather than claim literal completion or assign arbitrary splitting.
- Required outer wire/native/SQL fences still exist outside LockedStore/WireLog.
  They protect distinct authority/durability contracts. Earlier audit already
  qualifies the blanket lock grep; deleting those locks is not authorized by it.
- Original S1/S2 exhaustive captured-provider-stream before/after matrix, S3's
  one-to-one accounting of approximately47 checks, and S7's50/100/150-thread,
  three-poller latency/lock benchmark are **not established by the inspected
  receipts**. Current functional, native and capacity receipts are different
  evidence. No new paid runs, historical reconstruction or CI gate requested.
-255 reports focused/remainder progress, not full-suite green. Fix the concrete
  remaining failures against current owners; do not restore removed APIs or turn
  the old full-suite/CI language into a new deployment hold.

## Actual open-PR disposition

The attached snapshot records head identities and ancestry against `e6d7fe08`.
Recommendations only: this audit did not merge, close, rebase or edit any PR.

| PR(s) | Verified state / recommended disposition |
| --- | --- |
|229 | Keep as parent integration/quiet-activation owner until the named closures and live receipt. |
|234,235,236,237,240,244,246 | Every exact open head is an ancestor of229. Close as integrated/superseded with229 link; no second merge of stale branches.246's separately shipped unread work also has existing live receipts. Closing work branches is not claiming229 is live. |
|232 | All production/acceptance work through52581aa is in parent; only665bf1f deployment receipt was unique at snapshot. Retain receipt plus this audit, then close as carried by229. No further S13 implementation identified. |
|243 | Production2c03f7f integrated; only61e68302 independent capacity receipt is unique. Retain/link that receipt then close as carried by229; do not rebuild its old branch into a second runtime. |
|254 | Unique published ratchet implementation remains; integrate with paired Toad117. |
|255 | Now merged into229 at e6d7fe08. Exact stale manual/global-channel callers above remain in its merged checkpoint; route that bounded followthrough to Nietzsche/parent, not a duplicated implementation. |
|256,257 | Were forthcoming at assignment, now merged into229 at b58134fd/c3e7252a. No duplicate source work.257 package deployment remains pending. |
|108 | Unused advisory prototype: current production contains no `observe_selected_resource_claim` caller/API. Its body says it neither hooks the runner nor grants writes and keeps obsolete scans/adapters. Close as superseded/unshipped prototype; do not claim its distinct optional observation feature shipped. Current claimed-write authority stays with existing broker/claims. |
|115 | Do not merge old dict-event/operations-import patch. Two useful fixture intentions remain: canonicalize macOS `/var/tmp` in candidate tests; separate slow timeout from send-boundary probe. Route any retained current test corrections to255, then close115. CI-only draft hold is superseded. No current macOS run claimed. |
|Toad107 | Required current paired consumer; keep for parent integration/installed MCP/settings/current-core acceptance. Includes114/115 and merged118 plus119 source. |
|Toad114/115/119 | Already folded into107 per its current integration receipt; close as carried by107 once respective receipts retained. No new T7 work. |
|Toad117 | Paired shared-ratchet/test collection; remains active with core254. |
|Toad50/53/110/116 | Separate browser/discovery/frame investigation/workspace work; not original Comms completion blockers.107 explicitly excludes116. |
|Textual | No open PRs at this inspection. |

## Audit basis

Original files read under `/home/ts/wt/comms-refactor-dispatch-20260927`:
`plans/C0-carve.md`, S1–S8 migration/acceptance clauses,
`FINAL-ORIGINAL-PLAN-AUDIT.md`, `POST-PR95-DECISION.md`,
`POST-FEATURE-DEBT-AUDIT.md`; parent's deletion ledger, round2 L0,
`evidence/original-plan-completion/ORIGINAL-PLAN-COMPLETION-AUDIT.md`,
new `evidence/original-plan-final/AUDIT.md`, surface/native/D22 receipts;
current255/256/257/Toad107/118 handoffs and source diffs. Historical labels were
resolved against current source/ancestry. No whole-project zero-debt claim.
