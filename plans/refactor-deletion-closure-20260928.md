# Nominal refactor: plan, PR and deletion ledger

Updated 2026-09-28. Parent owns integration and installed acceptance.
The completion goal is active again: merged replacements do not certify that
every obsolete implementation, caller or compatibility branch was deleted.

## Authoritative plans and owner decision

Original plans are in `/home/ts/wt/comms-refactor-dispatch-20260927/plans/`:
`00-index.md`, `01-shared-abstractions.md`, `C0-carve.md`, and `S1` through `S8`.
`FINAL-ORIGINAL-PLAN-AUDIT.md` in that worktree defines R1–R7 caller closures.
`POST-PR95-DECISION.md` defines D1–D4; `POST-FEATURE-DEBT.md` and
`POST-FEATURE-DEBT-AUDIT.md` define PF1–PF5. Old progress headings in those
documents are historical; current assignment and deletion status is below.

Owner, 2026-09-28: "we hate legacy we hate backwrads comapt we cleanup in place"
and "cna ou se t a goal to push the refactor plans to compeltion including all deltion".
Newer instructions supersede original temporary re-export/mixin staging and CI gates.
Use existing declaration owners; remove replaced code and all current callers.
Preserve user data and UNKNOWN outcomes through explicit migrations, never replay.

## Existing implementation PRs

Numbers in this table refer to OpenHCSDev/agent-comms unless marked Toad.
These are landed implementation batches, not a claim of global zero debt.

| Original scope | Landed PRs / follow-through | Verified deletion or replacement |
| --- | --- | --- |
| A1–A10 shared abstractions | Foundation125, store129; respective S1–S8 owners | One family/codec/lifecycle/dispatch/identity/store contract reused across surfaces. |
| C0 composition and domain declarations |178/179 via180; paired Toad89 | operations.py and declarations.py absent; Comms is construction-only; package compatibility exports removed. |
| S1 internal events |127 via140 | Typed AgentEvent producers and consumers replace raw internal event dispatch. |
| S2 Pi boundary and turn lifecycle |143, then R1/196 | PiEvent/PiCommand/PendingRequests and typed payload owners replace repeated raw decoding. |
| S3 coordination state |151/165, then R7/201 and204 via206 | State rosters and obsolete state APIs removed; selected transaction and assignment vocabulary owned directly. |
| S4 routing/read/presentation |137/173, R2/192,205 via206 | ReadLedger owns displayed reads; duplicated builtin routing/name dispatch removed. Residual rows below reopen specific deletion gaps. |
| S5 identity and duplicate authorities |153/174,204 via206 | resource_claims.py absent; owner/turn identities and exact leases replace conflated counters/APIs. |
| S6 exports |125/158; paired Toad81 | Old export enums/factories/compatibility constructors deleted. |
| S7 residual owners |145/149/154/159/161/164/184; R3/R5/R6/R7 | ACP/session/turn/input ownership, WireLog/Publisher and document owners replace monolith/facade paths. Remaining obsolete paths below. |
| S8 goal lifecycle |134/166, R4/190 | Goal actions, generation/attempt owners and declaration-derived persistence replace scalar/transition mirrors. |
| R1–R7 complete caller batches |196,192,199,190,195,200,201; integration202/206 | Payload, catalog, input, goal, collaboration, transcript and selected-execution replacements; paired Toad consumers landed. |
| D1–D4 compaction |181/183,185 via186 | Old manual entry procedures, detached provider transport, string outcome adapter and raw lifecycle/witness mirrors removed. |
| PF1 tool-call lifecycle |215 | Parallel coding socket lifecycle collections/selected flags replaced by per-call owners. |
| PF2 private metadata/seals |217 | Raw marker/seal/witness schema mirrors replaced by declared owners. |
| PF3 native evidence |218 | Duplicate strict parsing and load_native_context_proof removed; remaining forwarding API is assigned below. |
| PF4 route observation |214 +Toad103 | Removed observation-time Comms construction and hidden UI polling. |
| PF5 history scope/index |211 | Removed per-row scope rebuilding and obsolete callback plumbing. |

## Round-two extension

The owner added plans/refactor2 and approved D22 on 2026-09-28. Canonical
rules/scopes/assignments are now docs/refactor/round2 (PR227). Those rules
supersede permanent archival converters suggested below: rewrite durable
history once in place, reset declared runtime stores at quiet installation,
and remove all old readers plus executed one-shot tools. Original closure
remains required.

## Remaining deletion assignments

Every row has an owner. A PR marked pending is NOT yet published; replace it
with its real number as soon as working source is committed and published.

| Batch | Owner / worktree | PR and state | Required closure |
| --- | --- | --- | --- |
| B1 canonical bus execution / S2,S4,S7,R3,R7 | Parent; persistent integration tree | Draft PR229; follows B2 writer caller; D22 approved | Delete public Publisher.publish, old append/sequence and InputDrain ACK/steer execution fallback; remove mark_view_read forwarding. Stage canonical roots through existing cutover owners, preserve original source/history and unresolved input provenance; no UNKNOWN replay. |
| B2 channel/catalog compatibility / S4,R2 | Darwin; ~/wt/comms-channel-deletion-closure-20260928 | Comms231 and Toad106 merged; quiet installation and tool deletion pending | Migrate saved union audiences/preferences/history into current owners, retire their active creation/routing, delete Channel.aggregate_target/members_for. Migrate update_tags to canonical publisher. Paired Toad callers belong to this batch. |
| B3 native evidence and response authority / PF3,S3,R7 | Pascal; ~/wt/comms-pf3-deletion-closure-20260928 | PR226 merged; installation pending quiet step | Delete _read_native_context_evidence after moving validation to NativeContextProof and migrating five current callers. Delete response-owner fixture bypass and optional authority forms; fixtures provide real witnesses. |
| B4 typed file-claim callers / S5 | Pascal; same authority PR as B3 | PR226 merged; installation pending quiet step | Move internal callers to ExistingFileClaim/WritableFileClaim; delete normalize_existing_file and raw internal coercion adapters; parse external paths once. |
| B5 disconnected bridges / S7,R6 | Parent; ~/wt/comms-acp-saved-session-startup-20260928 | PR225 merged and installed; local checks and installed read-only DM acceptance passed | Delete ordinary_delivery_bridge.py, transcript_route_legacy.py and their exclusive tests; preserve existing saved files and current native route/delivery owners. Fix remaining test caller of removed package export. |

B2 lands its publisher caller migration before B1 removes the old writer.
B3/B4 can land independently of B2. Parent serializes integration and activation.
No agent edits another owner's worktree. CI is deferred; focused local tests and
the affected actual installed path govern acceptance.

## Open PR disposition checked 2026-09-28

- Comms108: old advisory selected-resource observer prototype; no production
  caller in current source. Reconcile against current coding/claim owners before
  adopting any still-required behavior; do not revive its old adapters.
- Comms114: stale CI patch against deleted declarations.py and pre-PF2 marker
  dictionaries. Production changes are superseded; any useful test correction
  must be applied to current owners.
- Comms115: deferred macOS/timeout fixture corrections; old operations import
  and dictionary event assumptions require current caller migration.
- Toad53: command discovery design only; implementation remains a feature task.
- Toad50: local browser-serving gate; remains a separate feature/security task.
- Textual: no open PRs at audit time.

Issue107 native proof-journal growth, explicit native manual /compact, command
discovery and browser serving are not silently added as new feature promises by
this refactor goal. Overlapping obsolete implementations still require an explicit
retirement disposition. Historical saved-data decoders and actual external protocol
contracts are not blanket-deleted by searching for the word legacy.

## Acceptance rule

Close each row only after its concrete obsolete names/implementations are absent,
all current callers use the actual owner, saved data is preserved when affected,
local and installed-path evidence passes, and its PR is merged and installed.
Line deletion counts are evidence of removal, not proof of correct ownership.

## Current integration receipt

PR229 rebased onto main including231. Public writer/drain and duplicate scalar
read stores are removed. Current channel/read-ledger/idle suite: 35 passed,
6.63s (evidence/round2-l0/channel-current-contracts.log). Retained channel race
tests use the canonical writer and actual human provenance. Deleted only old
public-tail and missing-marker repair assertions. No live installation yet.
D22 history rewrite, old reader removal, admission floor across runtime reset,
and remaining current callers still belong to229/235. Round-two detailed
assignments are in docs/refactor/round2/04-DISPATCH.md.
