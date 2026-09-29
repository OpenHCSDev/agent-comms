# S2 terminal-result ownership — PR308 ready

Production checkpoint c14673f6, based on merged303/main664623a3. Parent owns
integration and final combined installed acceptance. No live install, native
package edit, paid provider call, input replay, or runtime reset.

## Ownership and deletion

Original S2 G1/G3 and S7 state-owning component closure:

- TurnOutput owns accumulated text/current message, provider errors, image
  diagnostic privacy, final assistant stop, diagnostic metadata and the winning
  TurnFailure. It reads actual input and lifecycle proof owners at settlement;
  no copied admission record or parallel process/session broker.
- Existing InputMissing, FinalStopMissing and QueuedInputMissing declarations
  now own terminal evidence detection and explanations through abstract
  TerminalFailure. The existing DeclaredFamily supplies membership; declared
  precedence supplies ordering. No manually maintained failure roster.
- Native retention consults terminal evidence before returning an idle child.
  A newly declared failure can reject success AND retention without editing
  backend dispatch. Cold NativeSessionPreparation retains its separate existing
  no-input readiness contract.
- Direct Pi event/command/input/watchdog/preparation consumers migrated together.
  ToolExecutionEnd uses its own local result instead of writing scratch values
  onto the session. This closes an actual first-pass migration collision.
- Deleted backend initialize_output, record_failure/fail_reason, duplicated
  terminal checks, ok flag, output/privacy scratch fields and terminal result
  scratch values. No aliases, compatibility readers or forwarding properties.
  finish_result remains the existing polymorphic preparation/ordinary result
  hook; it contains no result policy.

Backend1244 ->1126lines. Production250added/213deleted (net37): explicit state
ownership and declaration-owned terminal rules replace procedural duplication.
Tests184added/6deleted: four actual native cases plus deletion guards. The
existing failure-precedence behavior test now uses the actual output owner.

## Evidence

- recorded-corrected.log:243pass32.07s. External recorded protocol with real
  child pipes plus declaration/settlement tests; this is NOT native Pi proof.
- native-first.log:5actual pinned-Pi/localHTTP cases pass18.78s after the tool
  collision fix: tool error followed by a successful answer, retained-image
  privacy, provider failure, watchdog stall, selective steering interruption.
- native-final.log:c14673f6 production,5actual pinned-Pi/localHTTP cases pass19.95s:
  new failure declaration rejects success/retention, cold preparation retains
  without any provider call or input admission, tool failure recovers, retained
  image failure redacts, selected summary commits with one original admission.
  Exactly one native user start/history input on each ordinary single-input
  case. The inherited-image two-turn case reuses one child, then reaps it.
  Eight distinct actual cases across these two receipts; the unchanged three
  watchdog cases were not rerun after the final retention-hook addition.
- declarations.log:67pass0.92s, including failure precedence and deletion guards.
- lint.log and git diff --check pass.

Initial recorded-first.log remains FAILED:11failed232passed. ToolExecutionEnd
assigned a string to the new session.output owner; fixed by deleting those
scratch fields. The new actual tool test proves that path. No failure receipt
was overwritten or relabeled.

Both actual shards use the immutable native5fde package, local-only credentials,
localhost HTTP, disposable saved histories and bounded cleanup. They exercise
source code; no installed or live readiness is claimed. Parent performs the
combined installed short path. No repeat of the complete303 matrix.

Reproduce final source shard from this worktree:

    PI_COMPACTION_TEST_PACKAGE="$PWD/stack/.pi-native-5fdef596596173bd/node_modules/@earendil-works/pi-coding-agent" PYTHONPATH=src /home/ts/.local/share/agent-comms/runtime-cursor-recovery-20260928/bin/python -m pytest -o addopts='' tests/test_backend_native_output.py tests/test_selected_owner_compaction_integration.py::test_selected_native_summary_commits_and_admits_original_exactly_once --basetemp=.artifacts/terminal-results/recheck -q -s

## Architecture evidence and boundaries

Read original S2/S7, NRA skill and round2 rules. Full source plus dependency
context scanned before and after (audit-before.json/audit-after.json):22 raw
findings unchanged,21semantic_mirror_without_descent and1repeated_builder_calls.
No mapping_read/unmodeled_record_shape/redundant_type_check findings emitted.
CLI omits scan_status/analyzed/omitted detector counts; this is not a zero-debt
or complete detector-coverage claim. Raw JSONs and the migration recipe remain
under .artifacts/terminal-results. Semantic edits were authored target patches;
no NRA equivalence-proof claim. Actual runtime evidence is separately above.

Boyle cursor/store and Dalton S1 are untouched. Remaining backend launch,
attestation, retained-child custody and tool presentation are not claimed closed
by this slice. No broad baseline fixture porting or new generic registry.
