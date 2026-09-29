# Original S2/S7 tracked turn ownership — ready for parent integration

Implementation bd475eb7, caller correction93ee5daf, merged currentmain4510dddf
in74b5c848. Parent owns installation. Native package remains5fde.

## Owners and deletion

Read original S2/S7 in plans/nominal_refactor/files.zip and round2 rules.
TrackedTurnSession specializes TurnSession and uses existing MroDispatch
handler declarations for Response/InputCommitted/ContextCommitted/MessageUpdate/
MessageEnd/ToolExecutionStart/ToolExecutionEnd/AgentSettled. Deltas and assistant
messages dispatch through their existing nominal payload classes. The tracked
session owns witnesses, output, tools and child cleanup. It uses strict shared
PiRpcChannel framing and canonical PendingRequests correlation; no second
registry, raw decoder or event-name table. Latest context witness replaces the
formerly accumulated list. Ordinary ACP streaming behavior is unchanged.

NativePiRpcLaunch owns tracked construction and private environment policy.
run_native_pi_turn and prepare_native_pi_rpc_launch are deleted, including all
production/test callers; there are no aliases or retained procedural closures.
The only test references to those names are the explicit deletion guard.
Merged283 proof decoder is untouched. Dalton's merged287 tool implementation
is integrated through its existing public operations, without parallel owners.

Exact one-shot input/send fence, selected session startup fences, native proof
validation, original input identity, UNKNOWN/no replay, cancellation and package
trust remain required. No schema changes, native package mutation, paid call,
message152 replay or live deployment occurred in this sidecar task.

## Verification

- Source actual pinned native/localHTTP:9pass28.07s.
- Initial noneditable wheel:9pass30.83s.
- Final combined74b5c848 noneditable wheel:9pass28.79s. Actual2,098,094-byte
  record, cancel-large, EOF-large, configured loading/retained reopening,
  provider429/length, successful output and exact prompt rejection details.
- Combined tools/dispatch/deletion:72pass5.35s after merging latest main.
- Binding/refusal initial124pass1skip; three obsolete source fixtures fixed
  using current typed helper and3pass. Four additional same-session/renamed
  reservation cases pass2.07s. Selected preflight6pass.
- Remaining caller batch111pass1skip20fail178.65s. All20 failures reproduced
  identically on unchangedmain451cc423:19 obsolete inputDisposition expectations
  plus1 obsolete expectation that foreground stop creates no coordination DB.
- An earlier caller batch was interrupted for diagnosis:81pass10fail, never
  called green. Nine failures reproduced on unchangedmain451cc423:7 obsolete
  ACP cursor expectations,1 old error wording,1 incomplete package fixture.
  Our tenth failure was a migrated maintenance patch target, corrected and
  passing in the final111pass batch.

There are29 baseline failures, NOT a green whole suite. The baseline comparison
logs are preserved; no assertions were weakened or compatibility APIs restored.
Parent can track those existing projection/fixture cases with their owners.
Production lint and undefined-name checks over all edited files pass.

## Accounting and retained evidence

Compared with original451cc423, this change's source536added/442deleted (net+94),
tests297added/219deleted (net+78). Increase is explicit construction and declared
state/handlers; it is not deletion claimed from moving a procedure. Launch
factory84lines, execute62, complete50; no execution closure remains.

Final wheel: .artifacts/tracked-merged-wheel/agent_comms-0.1.0-py3-none-any.whl
Installed candidate: .artifacts/tracked-merged-installed
Final actual native log: installed-merged-native.log
The canonical Sol first harness receipt remains FAILED; the separate corrected
probe remains PASSED. Neither was rerun or rewritten. Parent's155/156 live proof
is separate from these provider-free localHTTP checks.
