# S9 PR236 — implementation in progress, not install-ready

Branch: `refactor/round2-s9-compaction-20260928`.
Owned tree: `/home/ts/wt/comms-refactor2-s9-20260928`.
PR: https://github.com/OpenHCSDev/agent-comms/pull/236

## Implemented source and deletions

- K1: PiCompactionSettings owns reserveTokens/keepRecentTokens and bounds;
  PiCompactionDecision inherits those fields to preserve Pi's flat native RPC.
  SelectedSettings, adaptive settings dict and copied manual token defaults deleted.
- K2/A14: PiHelper composes shared A12 BoundedRun with FieldCodec. Four actual
  package `.mjs` programs own settings, preparation, reopen and manual preflight;
  embedded programs removed. Existing import fence remains on native execution.
  Manual /compact retains its independently pinned stock-Pi authority and refusal
  for canonical native sessions; no guard bypass or new provider path.
- K3: all five CompactionJournal tables now derive DDL, records, reads, writes and
  indexes from A13. Stored lifecycle members replace status/commit/reason column
  mirrors. Deleted from_row/from_columns/load machinery and predecessor schema
  migration. Transactions, EXTRA/DELETE checks, directory fsync, uniqueness,
  input-before-journal locks and one-use returned ACKs remain the actual owners.
  Current A13 continued_private_session reader is integrated; fresh_private_session
  has no SQL table (its remaining exact native metadata seam is below).
- K4 manual: AttachedChild owns launch/finish/stop; local signal/group/shutdown
  implementations deleted. Consumer joins cleanup before releasing its session
  fence, including cancellation. Authority-child adoption remains below.
- K5: typed helper outputs, selected evidence envelope and declared native outcomes
  replace keyset parsers. NativeOutcome's state-specific fields replace raw evidence
  and the mirrored native_fields roster; committed digest/return-code checks remain.
- K6: admission source projection derives from its declared fields; commit consumes
  typed SummaryFiles/SummaryUsage directly instead of encode/redecode and repeated
  primitive checks. Payload/source/owner/correction/CAS checks remain.
- Unreferenced selected_source_snapshot and test-only exchange_fake_rpc deleted.
  No production caller used them. Obsolete compatibility/schema/parser/supervision
  tests are deleted; current native/durability/UNKNOWN behavior remains covered.
  Removed the old all-states commit_id/decline_reason placeholders as well; only
  the relevant lifecycle member carries each field. The affected current caller
  passes (terminal-field-caller.log); the larger run passed69 before that obsolete
  fixture assertion was replaced with its actual ReservedSummary contract.

## Current local evidence (overlapping batches are not additive)

- journal-boundary-current.log: **107 passed, 2 skipped**. Includes actual SQLite
  reopen/rollback/race/fsync, selected input/no-replay and returned-ACK behaviors,
  strict native receipts and A13 guards. The two existing opt-in native cases were
  not enabled in this source batch.
- native-current-boundaries.log: **7 passed**, actual pinned local native commit,
  metadata preservation and tamper rejection, pre-summary capture, saved-session
  reopen and new-case A14 test. Synthetic input only, no provider requests.
- a14-manual-current.log: **42 passed**, real stock Pi with loopback-only fixtures,
  manual refusal/no-retry, saved-session checks and A14. No external provider.
- a14-newcase.log: **1 passed**, one newly declared helper inherits actual child
  execution and strict result rejection without runner changes.
- wheel-helpers.log: all **4** `.mjs` files shipped, contents match source wheel.
- saved-journal.json: read-only SQLite backup from actual preserved private root;
  **96** old raw UNKNOWN markers retained unchanged, candidate refuses old schema.
  Other tables in this particular saved journal are empty; behavior evidence for
  their nonempty cases is the journal/native tests above. Original never written.
- owned-lint.log: current owned production/component/guard lint passes.
- guards-pending.log: intentionally truthful **3 failures**, exactly the remaining
  authority-child local supervision/deleted files and fresh native keysets below.
  These guards are permanent `refactor_guard` tests; no exception list.

Initial failed receipts retained locally: old native package fixture mismatch;
obsolete constructor/spawn mock; missing optional fixture model/window/settings;
transitional A13 API adoption. Current receipts above supersede these failures.
S9 is not complete while its guards fail. No CI wait, live mutation or deployment.

## Exact remaining shared seams and next edits

1. A12/Lovelace PR232: expose direct-parent inherited-authority child with existing
   launch gate + pidfd watchdog armed BEFORE exec release, retained authority FDs,
   deadline surviving owner death. Native commit compares parentPid to process.ppid;
   NamespacedChild cannot substitute a different PID/parent lineage. Request posted
   on232. Then S9 deletes owner_compaction_process's local supervisor and both
   compaction_child_launcher/compaction_child_watchdog files and their structural
   tests, consuming A12. Real owner-death/native authority test stays required.
2. S10 PR234: canonical strict startup model/thinking entry declarations + capability,
   including id/parentId/timestamp. Request posted on234. S9 will migrate
   FreshPrivateSession.verify_selected_startup and manual startup-tail verification;
   no second native entry registry or partial header decoder will be introduced.
3. A12 repeated cancellation cleanup fix is owned by Lovelace, already reproduced
   by S10; S9 consumes the corrected shared owner rather than another cleanup shim.

Dependencies integrated: main230; A13 committed native reader ownership d41b9ac;
A12 through175d188; S10 through6f25dc8. Their broader changes remain their owners'
PRs; parent integrates232/234 with236 serially. Selected RPC merge preserves typed
SummaryFiles/SummaryUsage, error causes and production persistent-child ownership.

## Quiet cutover (parent only)

Do not install this draft. When all source guards/shared seams pass, quiesce owners
and prove no compaction is in flight; preserve the old journal as evidence, reset
runtime compaction journal together with coordinated runtime input/admission state
at the reviewed no-replay highwater. Candidate accepts exactly the new A13 schema;
there is no in-src converter. Preserve wire/native histories/UNKNOWN evidence;
never replay their old attempts. Parent owns actual install/reset/relaunch and
configured-provider acceptance. Rollback selects old code plus its preserved old
runtime snapshot, with no automatic retry of anything uncertain.
