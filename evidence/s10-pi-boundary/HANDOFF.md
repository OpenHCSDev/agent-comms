# S10 V1–V3 and native child adoption complete; paired S9 integration next

Draft PR234: https://github.com/OpenHCSDev/agent-comms/pull/234.

Owner Pascal. Tree `/home/ts/wt/comms-s10-pi-boundary-20260928`, branch
`refactor/s10-pi-boundary-20260928`, source `18d1863`;
includes main `bf68bbb`, PR232 through `5d2935f`, PR237 through `9463a44`,
merged226, R0/228, channel231 and A13/230. Earlier receipts below are historical.
Parent owns whole-step quiet install. No live root mutation/restart/provider call.

## Implemented / deleted

- V1: removed `SelectedToolRequest.from_wire`, `from_arguments`, `argument_names`,
  duplicate-field decoder, exact-key lists and raw resource/content recovery.
  FieldCodec decodes SelectedToolEnvelope -> SelectedToolRequest ->
  SelectedWriteArguments. Declaration validates bounded relative text and owns
  parsed ExistingFileClaim; native lifecycle remains on NativeToolCall.
- Paired `selected_claimed_write.mjs` emits the current nested envelope. Old flat
  envelope is unsupported. Expected packaged source digest updated; package
  selection, peer PID, token, native start/event correlation, one-slot durable
  consumption and UNKNOWN/no-retry checks remain. No receipt grants authority.
- V3: ExtensionUiChoice ABC with Cancelled/Confirmed/Value variants owns the Pi
  response. Removed raw session.choice, repeated dict checks and per-key reads.
  TurnRunner.extension_ui_permission consumes the already decoded request and
  returns a nominal choice; two backend callback annotations migrate with it.
  Wrong-dialog or unavailable choices cancel. Correct confirmations/selections
  retain exact Pi RPC responses. No ACP lifecycle/process code changed.
- Includes PR226's guard-only follow-up, which arrived after its squash merge.
  PF deletion guards and S10 guards now use R0's refactor_guard marker.

## Acceptance (bounded, xdist disabled)

Python `/home/ts/wt/comms-refactor-integration-20260927/.venv/bin/python`,
`PYTHONPATH=src -m pytest -o addopts='' -n0`.

| Receipt | Result / scope |
| --- | --- |
| tools-first.log | 68 passed, one missing fixture import; fixed and covered below |
| tools-current.log | 24 passed, guard mistakenly included type annotations; corrected to executable body |
| tools-final.log | 25 passed: owner selected write, actual authenticated socket, shipped JS via Node, all 5 deletion guards |
| ui-current.log | 73 passed: Pi payload/RPC, real same-child UI pipe and ACP controller/revocation; 2 SDK cases lacked dependencies |
| ui-sdk-repair.log | both SDK cases pass using existing dependency directory, no install/copy |
| guards.log | 5/5 marked guards pass after R0 integration |
| debt-ratchet.json | PASS: type checks -11, string subscripts -7, long boolean chains unchanged |

Initial ui-first.log records an incorrect test filename; no tests ran. All failed
receipts retained. Socket/native fake cases include tampering, invalid proof,
forged terminal, denied authority, event order and cancellation. Real JS test
runs the shipped producer through Node against the actual authenticated owner
socket; no Pi/provider involved. Fake RPC cases use real child pipes, not an
actual model. Counts overlap and are not a summed suite total.

Prepared MCP dependencies were linked read-only from
`~/wt/comms-pi-mcp-client/extensions/pi-mcp-client/node_modules`; owned link and
short /var/tmp socket fixture are removed after tests. No duplicate native install.

NRA full context scan:79 detectors/0 omitted, exact_compact_global complete,
23.512s, four broad semantic-mirror findings at existing `_pi_mcp_live_receipt`,
`_publication_metadata`, `_read_snapshot_result`, and `TOOLS`. These are not
proof of new V1/V3 defects or a globally-clean result. No automatic codemod proof
is claimed. Manual ownership/caller closure and concrete boundary tests above.

## A12 adoption checkpoint (6f25dc8)

- Integrated PR232 into this branch. All three Pi-event `_terminate_process`
  imports/calls are deleted; failures use the actual AttachedChild.stop owner.
- Native tracked turns use BoundedRun.session for spawn/deadline/group retirement.
  Removed native_pi's terminate/killpg/wait/grace/cancellation-join mechanism.
  Raw prompt admission, proof checks and UNKNOWN handling stay in their owners.
- Full caller trace found selected_pi_child_deadline reachable only from
  SelectedSummarySlot.exchange_fake_rpc, itself used only by obsolete tests.
  Production run_selected_summary uses the persistent AttachedChild from backend.
  Deleted the whole 451-line guardian module, exchange_fake_rpc/_run_fake/_attempt,
  and three fake-only test modules. Namespaced supervision remains solely on
  A12's NamespacedChild, already covered by its real namespace/deadline tests.
  SelectedChildUnknown moves directly to selected_pi_summary_rpc; callers migrated,
  no alias/re-export. Production summary exchange behavior stays intact.
- Current native fixtures launch real AttachedChild processes. Removed old local
  group-signal variants; A12 owns those process semantics. Real Pi preflight
  fixtures replace fabricated process bags; actual current authority assertions
  remain. Summary fixtures now use the real persistent child owner too.

Additional focused receipts:
- child-adoption-first.log:70 passed/6 opt-in skips (native proofs, fake RPC,
  cancellation and actual failed-child reap before UNKNOWN slot release).
- event-summary-first.log:83 passed/2 opt-in skips;2 SDK failures were missing
  dependencies only. child-ui-sdk.log:both SDK cases pass after linking the
  existing dependency directory. Failed receipt kept.
- child-actual-cli-guards.log:7 passed, including actual prepared Pi CLI with
  loopback-only deterministic provider plus S10/A12 guards. No paid provider.
- child-main-native-seam.log:7 passed on current main, including the actual Pi
  read/edit/write/bash loopback fixture and six S10/PF guards.
- child-debt-ratchet.json:passes after current-main sync, including coupled A12
  changes: type checks -12, long boolean chains -1, literal subscripts -15.
  The pre-sync receipt failed +39 subscripts because origin/main advanced to
  include channel/A13 deletions; that receipt is retained as before-sync.json.
  The earlier handoff's pass label was premature and is corrected here.
- Ruff on changed source/current seam tests and git diff --check pass.

This adoption alone deletes804/adds252 production lines, deletes674/adds128 test
lines (not counting imported PR232's foundation). It replaces no live data.

## Current closure / integration ownership

V1, V2, V3 and native A12 adoption are implemented. V2 reads
NativeRuntimeInput.one and typed fields; the raw ten-key SELECT is deleted.
Cicero PR237 through9463a44 is integrated, including its runtime schema4 reset
and current assignment/generation fields. No old table reader remains in the broker.

Lovelace PR232 through5d2935f is integrated. The exact double-cancel reproduction
now prints child.alive=False before the caller finishes. The real native-turn
fixture covers one and two cancellations of a TERM-resistant process; both join
retirement before returning. No duplicate local cleanup algorithm was restored.

Existing NativeEntry now owns timestamp and an abstract StartupMetadataEntry
capability; ModelChangeEntry and ThinkingLevelChangeEntry share that family.
StartupMetadataEntry.read_startup(bytes) validates the complete declared external
record via FieldCodec, including duplicate keys, identity, parent and timestamp.
The ordinary history projection remains distinct from strict authority evidence.
Concrete matches_startup((provider,model),thinking_level) owns selection checks.
Darwin PR236 owns FreshPrivateSession/manual startup consumer migration; committed
API429232b and exact contract sent in issuecomment-5873631164. No edits to his
consumer files or second native entry registry. Those paired S9 callers must land
with this step; their acceptance is Darwin/parent-owned.

Latest focused receipts (counts overlap):
- v2-startup-current.log:30 passed, including exact native startup formats,
  selected broker/socket and the expanded whole-module V2 deletion guard.
- a12-repeated-cancel-fixed.log:exact original failed repro now joins retirement.
- native-closure-first.log:9 passed (actual prepared Pi CLI/loopback plus fake
  selected RPC pipes); actual four-tool test stopped at obsolete Thread(pid=...)
  fixture before native launch. Replaced those three _root fixture arguments with
  current ProcessIdentity.capture; native-closure-repair.log:actual Pi four tools
  pass, including admission, current typed native row, one publication and release.
- native-cancel-evidence.log:29 passed; real single/double native cancellation,
  strict evidence and shared A12 deletion guards.
- s10-closure-ratchet.json:PASS against mainbf68bbb at source18d1863;
  coupled dependencies type checks-14, long chains-6, string subscripts-195.
  This includes A12/A13/L0A changes and is not claimed as S10-only deletion.
- v2-startup-first.log is a no-tests invocation error (pytest-timeout unavailable);
  repaired command uses a bounded shell timeout. All failed evidence retained.

Current source is18d1863 plus the handoff/format-only completion commit. PR234 is
ready for parent coupled integration; no known S10 implementation blocker. Parent
owns full merged suite/quiet reset and actual installed acceptance. No live test,
provider send, deployment, process restart or user-state write performed here.

V2/startup/adoption-fixture closure alone:production+64/-15;tests+81/-8 before
format-only line splitting. Net source adds real strict external startup ownership;
V2 deletes the raw row parser. Earlier substantial guardian deletion is counted
above separately; imported A12/A13/L0A surfaces are not our own deletion claim.

## Stores and install boundary

V1/V3 change only transient socket/UI records; no saved/durable format changes.
Selected tool ledger stays runtime state. Imported S12 native runtime tables
use the current schema4; parent resets derived/runtime stores at quiet cutover. Wire/goal history and owner decisions untouched.
No converter or coexistence reader added. Pi RPC and ACP remain external formats.
Parent must rebuild/seal the prepared Pi package using the new shipped extension
before quiet step activation; an older selected extension is intentionally
rejected by current source validation. Do not patch the live package in place.

## Initial V1/V3 change accounting against R0/228 main

Production:127 added /113 deleted. Tests:252 added /56 deleted, including the
71-line PF guard carried after226. Source net growth expresses three distinct UI
decisions and declaration-owned broker arguments; tests add permanent deletion
guards and real producer/socket acceptance while deleting the old decoder test.
Exact paths are in CHANGED-FILES.txt. No compatibility preservation tests added.
