# Closure audit against assigned plans and DELETION-AUDIT

Audited main171 plus completed ThreadStatus172, then rebased this closure on main173 (`f5ddd74`), which includes both172 and Darwin's ViewPredicate/SavedView173. Consulted dispatch `OWNER-DECISIONS.md` top, `DELETION-AUDIT.md`, `00-index`, A1/A2/A3/A7/A8 shared ownership, and assigned S2/S3/S5/S7 requirements. Compared adjacent S1/S4/S6/S8 closure clauses against the actual merged owners. Old freezes, full-CI gates, alias preservation and hypothetical extension requirements are superseded.

## Required leftover found and closed

**S5 §4/§7: turn identity answers exactly which turn; migrate each scalar consumer. S7 §4/§7: Registration owns lifecycle, no forwarding facade.**

PR159 had removed scalar fence constructors, but left `Registration.finish_claimed_turn(name,id)` forwarding to `finish_claimed_turn_with_fence(name,id,expected=None)`. The document retained both ID-only and typed-fence release mechanisms; `Comms.finish_turn` exposed both. Production `Comms.begin_turn` rollback and `coordinated_runtime.run_one_sealed_claim` cleanup still chose ID-only release. A test existed solely to preserve the weaker legacy interface.

This branch removes both old registry/document method names and the optional/name/id Comms finish signature. `Thread.turn_lease` derives an immutable lease from that already-owned snapshot. Registration/document release take that exact lease; Comms.begin_turn captures it before activity publication, coordinated runtime retains it from successful admission, and TurnRunner passes its existing lease. No second counter or lease store. The compatibility-only test is deleted and real race tests replace it.

The closure exposed a genuine counter-ownership defect: `Comms.register` preserved an active executor but treated its newly constructed metadata declaration as a new owner, stripping admission from the retained turn. S5 says turns/metadata do not change owner identity. The existing no-executor-replacement path now preserves that lease and both counters. Inactive fresh registration still admits a new owner, and a different PID during an active turn is still refused.

## Audited completed surfaces

| Requirement | Current determining owner and deletion evidence | Result |
| --- | --- | --- |
| S1 event ownership / S2 internal event consumers | AgentEvent/MroDispatch, TurnProgress/OwnedTurn/AcpEventConsumer; no removed TranscriptUpdate.from_legacy path in current ACP components. | Already merged; no competing old event consumer found. |
| S2 §7 Pi readers, phase/failure/session owners | PiRpcChannel + PiEvent/PiCommand, TurnSession, TurnPhase, TurnFailure, input/stats/usage owners. `_stream_agent_events`, `_JsonLineReader`, `_ACTIVE_STEERING_TASKS`, `_SESSION_MUTATING_COMMANDS` definitions/accesses absent. | Closed143/164; no new mechanism required. |
| S3 §7 nominal states/codec/transition closure | Execution/obligation/claim/attempt/recovery families determine lifecycle and generated transitions; `state_tags.py`, old state enums/transition rosters absent. Records/projections use current nominal owners. | Closed165. |
| S5 identity split and Registration ownership | ThreadIncarnation/OwnerIdentity/TurnIdentity + GenerationCounter, RegistryDocument/RegistryStore/Registration; dead resource_claims and its test absent, ThreadRegistry/TurnClaimFence/epoch aliases absent. | Required release leftover closed here. |
| Relationship/passive138 data boundary | RelationshipDocument/PassiveAwarenessDocument; duplicate-edge raw views absent, explicit one-way relationship_migration preserves original revisions. | Closed169; migration/deployment parent-owned. |
| ThreadStatus / S4 residual status case recovery | ThreadStatus declaration family; public enum member/constructor/value API removed; parent owns applied paired Toad patch. | Closed172; included in current base. |
| S4 response policy / S6 exports | Actual declaration families; no upper-case ResponsePolicy aliases or enum scope/limit factories. Resolve methods on real scope classes are behavior, not old parsing adapters. | Closed158/164. |
| S7 ACP/runtime/CLI and S8 typed goals | SessionLifecycle/InputDrain/TurnRunner; RuntimeRequest/CliCommand; GoalAction/GoalState/PauseSource. Public ACP protocol methods remain genuine external entry points. | Existing merged owners; this branch only changes TurnRunner's single finish call. |
| ViewPredicate/SavedView | Darwin's173 is merged in the base. | Excluded from edits; no duplication. |

`deletion-audit.json` records the exact source-wide AST symbol inventory and absent files. Current Toad source has no turn-release callers, so this closure needs no new paired Toad edit. All src/test release calls were migrated; no removed member access or definition remains.

## Boundaries that are not obsolete interfaces

- Actual rename aliases preserve named user history. Native proof/journal decoding is a different external evidence boundary from Pi stdout RPC. Manual compaction's sidecar summary JSON and selected fake/deadline response are different subprocess protocols; deleting them as duplicate Pi readers would discard required protocols.
- The registry's existing disk keys `owner_epochs` / `owner_epoch_counter`, native receipt `owner_epoch`, and admission epoch fields encode external current data; they are not source aliases. Historical records without current turn attestation still decode as data and yield no lease. No old-client coexistence gate is kept.
- The remaining `WakeClaim` name and some local `epoch` variable spellings are live single owners, not retained duplicate declarations. S5's provisional terminology guard is not literally satisfied everywhere; a spelling-only campaign is not another substantive deletion surface.
- The original broad S7 proposal also suggested WireLog/Publisher extraction and deleting entire declarations/operations aggregators. Those have not all been implemented, and this audit does **not** claim that full architectural proposal is complete. They are retained original authorities, not old implementations behind replacements in the completed Registration assignment. The newest owner instruction now explicitly assigns the remaining C0 operations extraction as the NEXT task, with parent owning the updated plan. It is not claimed complete by this turn-lease closure.

After this concrete turn-release closure, the enumerated completed assigned surfaces have no further confirmed obsolete implementation/API requiring an independent deletion task. This is a bounded source audit, not a claim that all repository debt or every provisional plan sketch has disappeared. Parent retains live integration. C0 is now explicitly assigned next by the newest instruction; see the actual source inventory below.

## C0 coverage and next assignment

C0 was an explicit original Wave2 plan (`C0-carve.md` sections3A/3B/3C, index Wave2). Its full carve was not implemented. Source evidence in `c0-current-inventory.json` records all method locations, sizes, self-state accesses and self-call edges.

Actual work accomplished on that path: ThreadRegistry was deleted and replaced by real Registration/RegistryDocument/RegistryStore; goal/response/status/export families own substantial behavior; ACP moved to SessionLifecycle/ConfigOptions/InputDrain/TurnRunner. These are substantive S5/S7 extractions, not proof that C0 operations/model carving is complete.

Remaining original requirement: disjoint ownership/file regions for operations and declarations. Current Comms still spans **3,698 lines /153 methods** in operations.py; MessageBus **2,130 lines /75 methods** remains in declarations.py. CommsAgent is now **680 lines /26 methods**, demonstrating actual completed extraction on that side. Operations has no completed capability-module carve/composition root. The obsolete C0 recipe's shared-self mixins, permanent facade/re-export layer and full-CI gate are superseded by current owner decisions; do not recreate them.

Current operations hotspots for parent's authoritative planning: owner restart207 lines; rename transaction132; transcript-message assembly129; transcript paging119; goal waiter release109; owner launch99; live DM display97; stop95; public registration90; input routing repair80. Historical-display integration and reopened-viewer indexes are real newly integrated dependencies; component extraction must carry their existing owners and caches rather than fork them. New bus/coding/admission/compaction semantics must keep their current serial transaction boundaries.

Pascal takes operations-side implementation after this handoff. Parent owns updated C0/index/dispatch planning and paired integration; no duplicate edits to those planning files were made. Parent should use the inventory to assign declarations/bus regions independently. Direct current caller migration and deletion replace compatibility mixins/forwarding wrappers.
