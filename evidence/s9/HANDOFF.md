# S9 PR236 — source deletion and acceptance handoff

Branch: `refactor/round2-s9-compaction-20260928`.
Tree: `/home/ts/wt/comms-refactor2-s9-20260928`.
PR: https://github.com/OpenHCSDev/agent-comms/pull/236

## Source owners and deleted mechanisms

| Step | Actual owner / complete caller migration | Deleted |
| --- | --- | --- |
| K1 | PiCompactionSettings + PiCompactionDecision; selected payload, adaptive decision and native request consumers | SelectedSettings, copied manual token defaults and duplicate settings dictionaries |
| K2/A14 | PiHelper declares script/request/result, runs through A12 and FieldCodec; four packaged programs for settings/prepare/reopen/manual preflight | Embedded Python JS strings; dead selected_source_snapshot and fake exchange transport |
| K3 | Five CompactionJournal TypedTables and existing A13 native input readers; lifecycle declarations determine stored state/indexes | from_row/from_columns/load, raw row extraction, schema/column/status mirrors and schema conversion |
| K4 | A12 AttachedChild for manual Pi; BoundedRun.run_inherited for direct-parent authority with retained FDs and independent hard deadline | owner_compaction_process, compaction_child_launcher, compaction_child_watchdog; manual signal/group/shutdown code and structural tests |
| K5 | NativeOutcome family, helper result records and S10 StartupMetadataEntry; fresh/manual startup consumers use the single native family | Raw outcome/keyset parsers, native_fields roster, startup raw type dispatch and separate ID regex |
| K6 | SelectedAdmissionSource projection, typed SummaryFiles/SummaryUsage and committed native metadata; typed original-retirement witness | Repeated primitive validations, nullable terminal-field placeholders and inert correction_revision echo |

The correction_revision deletion touches only the corresponding Registration
attest/guard arguments, validation and returned receipt field. Canonical lock,
process identity, generation, goal/turn and native FD authority remain unchanged.
Actual wire/input correction checks remain on CompactionSource.

A12 through62adf9a and S10 startup429232b are integrated, including A13's current
native/coordinator tables. Their implementation remains in232/234/237. S9 does
not introduce another process launcher, native entry registry or SQL mechanism.

## Current local acceptance

Batches overlap; counts are not additive. All requests below were local fixtures,
with no external provider calls, live owner changes or native package mutation.

| Receipt | Result and actual boundary |
| --- | --- |
| guards-current.log | **3 passed**, zero exceptions including manual bridge; no local supervision, embedded JS, exact keysets, raw SQL rows or duplicate settings owner |
| current-owned-lint.log | Owned S9 production and guard lint passes |
| a12-native-authority.log | **36 passed** actual pinned native CAS, metadata/tamper checks, owner SIGKILL before/after write, retained locks, timeout and UNKNOWN/reconciliation |
| a12-authority-first.log | **38 passed** authority span, package verification and typed native outcomes |
| a12-cancel-native-current.log | **3 passed** owner/inner/all-task cancellation: commit settles before turn lock release; corrupt reopen refuses provider launch |
| all-native-helpers.log | **14 passed** real pinned settings/preparation/reopen helpers and A14 new-case strict result contract |
| final-source-boundary.log | **76 passed, 1 deselected** journal/returned ACK/owner/guard behavior; exact ACP dependency failure recorded separately below |
| startup-contract-first.log | **66 passed, 2 opt-in skips, 1 failure**; strict fresh startup and S9 guards pass, ACP fails in shared ThreadManagement before compaction |
| typed-original-retirement.log | **18 passed, 32 deselected, 1 same ACP failure** after replacing raw retirement witness key reads |
| a14-manual-current.log | Earlier **42 passed** actual stock-Pi/manual/A14 batch, BEFORE stricter startup declaration adoption; does not supersede current manual failure |
| wheel-helpers.log | All four shipped `.mjs` programs match source in built wheel |
| saved-journal.json | Actual read-only saved journal backup:96 old raw UNKNOWN records unchanged; candidate refuses old schema; other four tables empty in that saved source |

Initial failed receipts are retained. The first cancellation rerun patched all
asyncio child creation and consequently blocked the new read-only A14 child; the
corrected test intercepts only the actual provider launcher, retaining real helper
execution and the no-launch/no-mutation assertion. Deleted manual supervisor tests
are not ported to A12 mocks.

## Concrete remaining integration acceptance

1. **L0A235 / A12:** ThreadManagement.claim_thread still constructs Thread(pid=),
   although Thread now owns ProcessIdentity. Actual ACP new_session fails before
   S9 executes. Reported directly on232/235; no constructor alias or local patch
   is retained in S9. Integrate the existing owner's direct caller repair, then
   rerun the affected ACP admission/manual-bridge paths.
2. **S10 strict startup declaration:** its8hex parentId constraint rejects actual
   stock-Pi metadata appended to a saved session with UUID entry IDs. Eight real
   manual loopback cases fail before any HTTP request. S10 owns the external parent
   string/null contract repair; fresh S9 attestation separately compares exact
   bootstrap/model parent identities. startup-manual-real.log retains the failure.
3. **S10 generic-text retirement:** manual_compaction's single rpc_args_for consumer
   is being moved to NativePiRpcLaunch.rpc_arguments. Removed local --print
   allowance and its fixture argument. The shared methods are in Pascal's current
   code; the paired callers are published in236 and require his committed contract
   before execution. S9 also removes the adaptive basename gate so a verified
   route-selected pi/directcli cannot silently disable compaction.
   The canonical manual-bridge refusal must also recognize route-selected `pi`
   and a direct pinned cli.js, not only two executable basenames. Exact authority
   retention/consumer seam sent to Pascal; no second classifier will be created.

S9 is not install-ready while those real paths fail. Source guards alone do not
certify the runtime. There is no CI wait and no request for new paid acceptance.

## L0 literals and policy boundaries

See L0-BOUNDARIES.md. The actual Pi dependency `openai/internal/shims.mjs` and the
pinned native digest prefix `agent-comms-metadata-v1\n` are external literals,
not old internal identifiers/comments. They remain exact. Manual stock-Pi
/compact and journaled owner compaction remain distinct; canonical-native manual
refusal stays in force. No unjournaled writer or provider retry is introduced.

## Quiet cutover — parent only

Store classification: `compaction-commits.sqlite3` (all five declared tables) is
runtime exclusion/receipt state and is archived then reset. Native session JSONL,
its `.input-proof` journal and the wire remain durable history/evidence and are
not reset by S9. Temporary manual profiles are disposable and removed after A12
has retired the child; shipped helper programs are package code, not caches.

Quiesce owners and prove no compaction is in flight. Preserve the old journal as
evidence; reset runtime journal together with coordinated runtime input/admission
state at the reviewed no-replay highwater. Candidate accepts only its A13 schema;
there is no converter in src. Preserve wire/native histories and UNKNOWN evidence;
never replay old attempts. Parent owns actual reset/install/relaunch/acceptance.
Rollback selects old code with its preserved runtime snapshot, without retrying
uncertain work.

Authored source/test counts exclude dependency merges (change-counts.json):
production907added/1960deleted; tests332added/1232deleted at ebe49b5. Counts will
be refreshed after the final paired launch-contract consumer lands.
