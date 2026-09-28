# S10 V1/V3 + A12 adoption; V2 and shared cancellation closure open

Draft PR234: https://github.com/OpenHCSDev/agent-comms/pull/234.

Owner Pascal. Tree `/home/ts/wt/comms-s10-pi-boundary-20260928`, branch
`refactor/s10-pi-boundary-20260928`, source `6f25dc8`, includes PR232 through `175d188` and merged226 + R0/228.
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
- child-debt-ratchet.json:passes against main including coupled A12 changes.
- Ruff on changed source/current seam tests and git diff --check pass.

This adoption alone deletes804/adds252 production lines, deletes674/adds128 test
lines (not counting imported PR232's foundation). It replaces no live data.

## Remaining assigned scope / exact dependencies

**S10 is incomplete.** V2 remains the ten-column raw native_runtime_inputs read
in selected_tool_broker.verify_sent_full_input. Cicero PR230 owns A13 and the
canonical NativeRuntimeInput declaration; adopt that landed API here, not a copy.

**Concrete A12 cancellation gap for Lovelace PR232:** two cancellations of a
BoundedRun.session containing a TERM-resistant child allow the caller to finish
before the retained stop task reaps the child. Reproduced locally; see
`a12-repeated-cancel.log` and `repeated-cancel.py`. Child was then fully reaped by
its owner. Native's replaced cleanup previously guaranteed join before returning,
so coupled integration needs A12.stop/session to preserve that invariant under
repeated cancellation. No private workaround/second cleanup algorithm added here.
Parent was notified once; no live reproduction or installed change.

PR232 alone removes backend helpers required by old pi_events; integrate this
branch's migrated callers with it. Parent handles serial coupled merge/full
merged suite/quiet step activation. Guards cover current V1/V3 and retired local
supervision; they do not claim V2 or the shared cancellation issue complete.

## Stores and install boundary

V1/V3 change only transient socket/UI records; no saved/durable format changes.
Selected tool ledger and native runtime inputs are runtime state; their formats
are unchanged in this draft. Wire/goal history and owner decisions untouched.
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
