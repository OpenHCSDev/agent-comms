# Original-plan deletion/caller audit, 2026-09-28

Baseline: parent `1aef899`; manual-route followthrough based on parent `9e3dc3c`.
Maps read: original dispatch FINAL-ORIGINAL-PLAN-AUDIT.md, C0/S1-S8 and shared plans,
POST-PR95-DECISION.md, POST-FEATURE-DEBT-AUDIT.md; parent
plans/refactor-deletion-closure-20260928.md and original-plan-completion audit.
Newest original/no-compatibility decisions supersede historical staging allowances.

## Concrete gaps

1. **S7/D1/L0 retired stock manual writer — Cicero, this followthrough on253 branch.**
   `manual_compaction.py:34,192,305` still held the 256 MiB whole-file reader,
   pinned stock provider route and ManualCompaction transaction. The production
   caller disappeared in251: ACP.prompt/runtime CompactContextRequest call
   manual_compaction_bridge.compact_context, which calls compact_manual_owner.
   Exact tracked imports/callers found only tests. Deleted the whole module and
   its exclusive manual_preflight.mjs. PiHelper's optional import fence served
   that stock route; all remaining production helper callers validate their
   native package, so removed that branch too. No cap was increased.
   Removed exclusive obsolete tests. Moved the existing LoopbackProvider into
   tests/compaction_loopback.py for the surviving canonical retained-session
   acceptance; no replacement provider or runtime path.

2. **Mixed ACP test caller closure — Nietzsche, existing248.**
   At baseline: tests/test_acp.py:194,237,314 still patch
   manual_compaction_bridge.manual_compaction.ManualCompaction.run, and
   tests/test_acp_compact_command.py:187 patches the retired writer. These mixed
   files are already in248's current write set. Sent exact crossing on248:
   https://github.com/OpenHCSDev/agent-comms/pull/248#issuecomment-5876688919.
   No duplicated implementation or overwrite of Nietzsche's in-flight fixture
   edits. Full combined test closure is not claimed by this deletion commit.
   `tests/test_view_unread.py:112,121` also names removed mark_view_read; this
   remains248's current fixture closure, not a request to restore the adapter.

3. **S4/R2/L0 broadcast alias — parent229/B2 disposition required.**
   docs/refactor/round2/L0-legacy-sweep.md:29 explicitly requires deleting the
   broadcast alias. Current `channel_targets.py:15-16` declares it;
   `lookup/canonical/is_alias` at23-34 keep it active. This is not only archival
   recognition: Publisher._validate_publish_request at98-108 exempts it from
   ordinary target validation and _prepare_message_unlocked at139 canonicalizes
   it for NEW writes; publish_initial_cohort also canonicalizes at366.
   Parent ledger's original S4 closure preserved aliases historically, but the
   newer L0 rule supersedes that allowance. No explicit new deletion PR is listed
   for this surviving route. Parent owns D22 retained-target interpretation and
   B2 reconciliation; do not blindly remove original archived rows or their
   display inclusion. This is a concrete source/plan contradiction, not a word
   count. No alias changes made in this audit.

## Required original ownership map checked

| Plan scope | Current source / deletion trace | Remaining owner |
| --- | --- | --- |
| C0 | Comms construction root; operations/declarations/resource_claims aggregate modules absent; explicit components | No new missing component established |
| S1/S2/R1 | AgentEvent/MRO consumers; PiEvent/PiPayload/PiCommand, PendingRequests; no old dict-event, Mapping PiEvent, old line-reader or stream procedure | Parent native integration;248 fixtures |
| S3/S5/S8/R4/R7 | Declared lifecycle families, typed goal tables, one SelectedExecution and exact turn leases; old WakeClaim/ClaimState/ThreadRegistry/procedure names absent | No new state/transaction replacement required |
| S4/R2/PF4/PF5 | CatalogDocument/ChannelCatalog and ReadLedger own facts; resolve_comms_route avoids Comms construction; historical_page captures scope once and uses scope.index_targets | Broadcast contradiction above; Copernicus107 paired UI |
| S6 | Current export families and callers; old kind factories/metaclass absent | No new export replacement required |
| S7/R3/R5/R6 | InputDispositions/RuntimeInfoStore/SharedLedger use LockedStore; NativeEntry/NativeTranscript and current transcript owners; no original raw document aliases | Parent native243/244 integration;248 fixtures |
| D1-D4 | Canonical manual owner plus three journal state families, typed NativeWitness and selected summary transport; old detached summarizer/PreparedOwnerSummary absent | Stock writer deletion above; parent253/D22 integration |
| PF1/PF2/PF3 | NativeToolCall/OwnerToolSocket shared by selected and coding policies; WireMetadata/PrefixWitness/seal owners; NativeContextProof.read_evidence with typed NativeEntry | No additional unowned mechanism established |

An AST name audit at exact1aef899 found zero references to the explicit original
retired set (WakeClaim, ClaimState, ThreadRegistry, TurnClaimFence, ProjectionRecord,
PreparedOwnerSummary, _JsonLineReader, run_one_sealed_claim, _stream_agent_events,
_read_native_context_evidence, load_native_context_proof, normalize_existing_file,
initialize_private_claim_protocol, _private_session_mode). This proves only absence
of those names; it is not a zero-debt or full behavioral-equivalence claim.

## Focused proof and unchanged ownership

manual-native.txt:8 passed in6.88s. Includes current S9/native deletion guards,
actual child helper new-case/strict decode, missing import fence refusal, real
prepared-native session reopen retaining bytes/identity and read-only Pi settings.
Only these affected helper checks were run; no replay of the accepted large-file,
D22 copied-root or full manual provider acceptance. R0 receipt accompanies commit.
No live state/install/restarts, paid provider work, or other worktree edits.

Parent owns D22/native integration and final one-shot tool deletion; Nietzsche248
owns merged-suite closure; Pascal254 owns TR0; Darwin owns T1; Copernicus107 owns
paired Toad. Historical universal size/lock guards and exhaustive real-stream/
50-150-thread experiments remain explicitly qualified in prior original audit;
this pass neither repeats them nor claims they passed. No new hold or polish task.
