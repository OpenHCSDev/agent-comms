# Current original + round-two completion/deletion map

Audited committed core main `46b729cf16812620cf9d419aa30d95450a87687e`,
2026-09-28. Toad remote main inspected at
`c3f7b632afce677b66d280ca29531e7879a66236`. This supersedes the **remaining-work
snapshot**, not the historical receipts, in
[`s13/original-plan-audit/AUDIT.md`](../s13/original-plan-audit/AUDIT.md).

Main advanced during the audit: merged277/279 private queue coverage and prompt
refusal changes were incorporated; both reader findings remain present. Preserved
NativePiPromptRejected and canonical config-directory fixes. No assigned work duplicated.

Method: original C0/S1–S8 migration and acceptance clauses, original D/R/PF
closure audits, round-two rules/index/L0/R0–R1/S9–S13 and dispatch, committed
source/callers/deletion inventory, existing installation receipts and actual
open PRs. Reused the existing original source-inspection operator; 19 relevant
current source guards passed (see exact final duration in source-guards.log). No new provider run, installation, native
history mutation, whole-suite claim or complete NRA scan. Source checks prove
the mechanisms they inspect; they do not reconstruct historical experiments.

## Concrete independent closure queue

These are bounded **unowned review/implementation items**, not an assertion
that removed code should return. Audit owner owns this mapping; no second
implementation has been started. Parent can dispatch these exact scopes.

1. **Native proof-journal boundary: finish shared A2 decoding.**
   `src/agent_comms/native_pi.py:124`, `NativeContextProof.from_journal`, still
   hand-builds the exact key set, reads schema/type by string keys, retrieves
   `FieldCodec._types`, loops fields and calls `FieldCodec.decode` separately
   for each field. The same existing proof declaration owns the facts, but
   this is still a hand-written record decoder. It remains called by
   `_verified_history_rows`; it is live and cannot simply be deleted.
   S10's “nothing decodes by hand” and A2 ownership are therefore not fully
   established by its narrower broker/UI guards. Close by putting the current
   journal envelope/schema validation on the existing declaration boundary,
   decoding once through canonical FieldCodec, and removing this hand mapper
   and its internal codec access. Preserve strict duplicate/unknown fields,
   private file revision checks, tracked-entry corroboration, generation/digest
   agreement and absence of replay authority. Acceptance needs the actual pinned
   native proof journal through tracked execution/reopen, plus malformed-input
   negatives. No old-format reader, new codec, or provider retry is required.

2. **Tracked native RPC record capacity: remove the separate fixed rejection.**
   `native_pi.py:42` declares `_MAX_LINE = 1 << 20`; tracked execution passes it
   to `PiRpcChannel.readline` and `decode_record` and independently rejects
   larger complete records at lines 816–840. Canonical `PiRpcChannel` already
   assembles complete records across transport-buffer overruns; ordinary
   backend readers use that path without a complete-record ceiling. This is
   a remaining capacity followthrough, not an obsolete compatibility path.
   Close through the same channel with bounded chunk/read lifecycle and strict
   framing; keep bounded stderr observation separate from RPC acceptance.
   Prove a real pinned-native large response/tool record survives with context
   evidence, cancellation/EOF settles and input is not replayed. Do not replace
   1 MiB with a larger constant. The old transcript-tail dropping cap is already
   gone: `NativeTranscript._reverse_records` reads complete records, independent
   of its 64 KiB scan buffer. Don't reassign that completed replacement.

Neither item edits future-summary reservation/cut selection (Wegener152/277),
wake visibility (Boyle), Toad127 (Carver), App/Tab/clipboard (Noether) or
workspace116 (Tesla). Both concern the tracked native reader/proof boundary;
they can share a single scoped core implementation with no Toad wire change.
No stored format change or reset is authorized by this audit.

## Older blockers: current disposition

| Previous audit blocker | Current merged source / existing receipt | Disposition |
| --- | --- | --- |
| Selected extension digest mismatch | Current pin and broker shipped with original/round-two integration; actual selected tools, retained manual/adaptive, MCP and later paired-native receipts are recorded in `evidence/t2-boundary/FINAL-ACCEPTANCE.md`, `evidence/compaction-failure-recovery/LIVE-RESULT.md` | Historical mismatch is not a current unowned package assignment. No old immutable bundle is used for this audit. |
| PR257 deployed inventory capability deletion | Current integration and D22 pin receipts supersede pre-install source snapshots | Don't recreate deleted fields or reopen257. This audit makes no fresh deployed-file inventory claim. |
| Manual writer test callers | Old ManualCompaction patch/import paths absent from current ACP mixed callers; canonical `compact_manual_owner` path remains | Closed source/caller deletion; not permission to delete real compaction behavior tests. |
| Global `broadcast` alias and historical tests | No old alias target in the named mixed-test callers; literal English “broadcast” in comments/test descriptions remains | Alias closure accounted for; don't confuse prose or an actual user-named thread with the removed route. |
| D22 rewrite, UNKNOWN preservation and tool deletion | `evidence/d22-quiet-stop/LIVE-RESULT.json`:149 current +8420 archived messages,104 threads,13 unresolved inputs, zero replay, four owner attachments; `tools/cutover` absent | Completed recorded installation, not a reason to reinstall/reset data. |
| Packaged ratchet / paired117 | Shared `debt_ratchet.py`, installed console entrypoint and manual workflow; original script copy removed; Toad viewport ownership integrated | Source closure complete. Required-CI language superseded by owner; CI deferred. |

## Original requirement → current owner and deletion

“Accounted for” means the named replacement and predecessor deletion are found;
it does not certify every historical experiment or a whole-project zero-debt state.

| Scope | Current ownership / deletion trace | Remaining qualification |
| --- | --- | --- |
| A1–A10 / C0 | `declared_family`, `field_codec`, `mro_dispatch`, `locked_store`, command/update families; `comms.py` construction root. `operations.py`, `declarations.py`, aggregate imports absent | No temporary re-exports/mixins to retire. |
| S1 / A10 | `AgentEvent`, `AcpEventConsumer`, participant consumer, shared turn settlement; producer updates decoded by existing MRO owners | Historical before/after real-stream equivalence matrix not reconstructed. |
| S2 / R1 | `PiRpcChannel`, `PiEvent`/`PiPayload`, `PiCommand`, `PendingRequests`, `TurnSession`, `TurnPhase`, `TurnFailure`, `UsageAccount` | Native private-journal decoder item1; record-capacity item2; historical exhaustive transcript matrix unproven. |
| S3 / R7 | Assignment/execution/attempt/obligation families; selected execution transaction and declaration-derived tables; old WakeClaim/ClaimState/ProjectionRecord/run_one_sealed_claim absent | Exact original ~47-legality-check accounting not supplied by this audit. New current typed cases are not backward compatibility. |
| S4 / R2 | `ReadLedger`, display bases, `ResponsePolicy`, catalog and message owners; scalar marker/forwarding/alias route retired | Existing read/rebind receipts retained; no new randomized read property run. |
| S5 | `ThreadIncarnation`, owner/turn identity and `TurnLeaseFence`; dead resource_claims module/test and epoch identifiers absent | No independent duplicate ownership authority found. |
| S6 | Export scope/limit/format families and direct CLI/Toad callers; retired kind enums absent | Old constructor/golden compatibility provisions superseded by round-two rules. |
| S7 / R3,R5,R6 | Session lifecycle, input queue, turn runner, configuration, registration, WireLog/Publisher and typed document/transcript owners; old façade, raw backend, delivery/transcript bridges and manual writer absent | Literal size/lock/performance clauses below remain qualified; do not remove real transaction fences. |
| S8 / R4 | GoalState/GoalAction/GoalExecution, generation and attempt owners, typed history; retired state loaders/cursor/grant-adoption paths deleted | Current goals use the existing shared owners; no new goal mechanism. |
| D1–D4 | Canonical manual/adaptive owner compaction, typed operation/summary/publication lifecycle, witness and selected summary transport; stock manual writer/detached provider and string outcome adapter retired | Retained-session native commit/reopen and actual provider fork receipts exist; don't redo unchanged paths. |
| PF1–PF5 | `NativeToolCall`; WireMetadata/PrefixWitness/seals; `NativeContextProof.read_evidence`; validated route observation; captured history scope/index | PF3's shared proof reader is live; item1 closes its remaining hand decoder, not a second validator. |

## Round-two clause map

| Scope | Current source and deletion | Current acceptance / remaining |
| --- | --- | --- |
| L0A | Current-only goals/registry/thread/catalog loaders; old owner epochs, scalar marker, channel audience compatibility and archived format converters absent | Named L0A/channel guards pass. |
| L0B / D22 | Canonical writer/native drain; `supervised_cutover.py`, old public publisher and executed converters absent | Named L0B guard passes; recorded actual D22 receipt preserves history/no replay. |
| S9 K1 | One `PiCompactionSettings` in owner_compaction_settings; native actual settings and typed decision consumers; old manual defaults/writer deleted | Current S9 guards pass; cold settings/native acceptance is already recorded. |
| S9 K2/K5 | Shipped `_pi_helpers/*.mjs` through PiHelper typed request/result; Python JS strings/key-set parsers retired | Existing actual helper/retained-session receipts retained; no new helper required. |
| S9 K3 | Journal/session rows use TypedTable declarations; current journal reset recorded with272/273 integration | Don't reset durable history or rerun a quiet cutover for this audit. |
| S9 K4/K6 | A12 child ownership replaces lifted watchdog/supervisors; typed admission/commit consumers | Named S9 guards pass; don't restore the deleted compaction launcher. |
| S10 V1/V2/V3 | SelectedToolRequest socket decoded via FieldCodec; NativeRuntimeInput.one; typed ExtensionUiChoice | Three guards pass, but their file/operation scope does **not** certify item1's private proof decoder. |
| S12 / A13 | TypedTable owns package-wide SQLite DDL/writes/reads/projections; duplicate lifecycle row mirrors/mappers gone | Both package-wide guards pass; existing D22 durable conversion/reset receipts account for install. JSON `row[...]` in a private journal is not a SQLite violation. |
| S13 / A12 | ChildProcess/AttachedChild/BoundedRun/DetachedProcess, typed ProcessIdentity and capability-composed Platform; all inventoried actual spawn/signal primitives are in child_process.py | Three guards pass; live owner relaunch receipts exist. Full Darwin/native Windows package execution still not established; Linux/Wine evidence has its stated limits. |
| R0 / TR0 | Shared packaged declaration-owned ratchet including per-class size, no script copy/manual case roster; workflow_dispatch | Required automatic CI is intentionally superseded, not a shipping blocker. |
| R1 (NRA) | Prior merged NRA9 calibration reports193 files81/81 detectors without omissions; skill asks for raw-record findings | This audit did not rerun NRA at current head; no current complete-scan or ≤25% timing-growth claim. |

## Literal requirements still not established

- Existing source inspector reports **8 modules over1000 lines and35 functions
  over100 lines**. These original universal caps are still false. Counts alone
  don't choose component ownership or justify arbitrary splitting; Toad T4's
  assigned work is excluded. Do not label global original structural completion.
- S7's universal lock restriction remains false: wire/registry/native transaction
  fences occur outside LockedStore. Prior audit distinguishes real durability and
  authority fences; no evidence authorizes deleting them merely to satisfy grep.
- Original S1/S2 exhaustive old/new replay matrices, S3 exact legality accounting,
  S4 randomized read-property experiment, S7 50/100/150-thread three-poller latency
  and lock benchmark and full cross-platform A12 execution are not established by
  the read receipts. Historical acceptance qualification is not a current live bug.
- L0's literal zero-marker guard is not package-wide. Current matches are the
  external Pi `fallbackTransport` field/display and the external error decoder's
  local `fallback` variable. Neither is an old internal format reader. Deleting
  Pi's recorded transport fact would weaken truthful feedback. This is an explicit
  external-contract qualification; no cosmetic rename batch was manufactured.

## Evidence and next action

`source-inspection.json` records zero retired identifiers, epoch identifiers,
aggregate imports or lease/assignment remnants. `boundary-inventory.json`
records additional deleted modules, absent cutover tools, process primitives,
lexical matches and exact two independent boundary sites. `source-guards.log`
records19 passing current guards, with source imports explicitly selected.
The static private-function reference check found no private top-level function
with a sole production-name occurrence; that limited check is not whole-program
dead-code proof. No genuinely dead production surface was established, so none
was deleted speculatively. Source/test change count for this audit: **0/0**.

Publish this map, then assign the two concrete reader/proof closures together if
parent confirms no new owner has claimed them. Preserve existing assigned work:
Wegener152/277, Boyle wake visibility, Carver127, NoetherApp/Tab/clipboard,
Tesla116. Current open core snapshot has280 (Boyle);277/279 merged during the audit. Toad snapshot has110/116/126/127.
These snapshots establish PR state only, not whether every active turn is useful.
CI is deferred; no additional gate or owner-permission ceremony is proposed.
