# S2 native launch, attestation and retained-child custody — PR311 ready

Production checkpoint fc73cb8d. Based on308, now merged with current main307/
308/306 atc338e8ab via b6a50118. Parent owns review/install and combined installed
acceptance. No live install, native package change, provider charge or input replay.

## Coherent ownership and deletion

Original S2 turn-session ownership and S7 state-owning component closure:

- PersistentPiSession owns the actual ChildProcess, PiRpcChannel and stderr task
  throughout launch, turn use and idle retention. A turn borrows that owner,
  including for non-retained runs; it never copies those handles back on success.
- The same owner decides auth/revision reuse, strict saved-session reopen,
  startup admission, process launch and metadata retention. Its existing lock,
  writer fence and independently shielded close task remain the custody boundary.
  stderr capture binds the actual stream before scheduling, so early retirement
  cannot redirect a newly scheduled reader through a cleared owner field.
- NativeAttestation owns the correlated GetState request and actual StateData
  response, checked against the existing NativeSessionIdentity where required.
  Capability readiness derives from that response. Identity rejection owns its
  reaction through IdentityAttestationError; input uncertainty is not reused as
  a proxy for identity failure.
- Deleted TurnSession.attest_input/validate_reopen/spawn_child/stderr_tail, the
  duplicated process/reader/stderr fields, mutable capability flag, preflight-ID
  copy and retained-resource assignments. Cached reused/validated-session fields
  are gone entirely; those decisions stay local to opening the native owner.
- Direct Pi event/command/input/watchdog/output/stats/preparation callers migrated
  together. NativeSessionPreparation uses the same owner and remains able to
  retain an attested idle child without sending any input.
- Failed/cancelled turns close the actual owner in finally. Only verified retained
  turns preserve it. New tests assert custody already belongs to the saved-session
  owner at native input start, rather than being reconstructed at the end.

No new flags, compatibility aliases, process broker, parallel pending registry
or old-method forwarding facade. The obsolete backend capability re-export was
removed; all test callers import the native declaration. Existing task cleanup
indexes/API remain unchanged. Boyle runtime dispatch/bootstrap/worker surfaces
and Dalton S1 are untouched; boundary coordination is on308 and311. Main merge
had no overlap in this slice.

Backend1126 ->1064lines. Production297added/280deleted (net17): the explicit
attestation owner and typed rejection replace dispersed state and copies.
Tests119added/9deleted: four actual custody cases, stronger live resource checks,
deletion guards and direct constant-caller closure. No store formats changed.

## Focused and actual evidence

- recorded-first.log:196pass28.58s;7native-helper cases skipped because that
  initial recorded-protocol shard did not configure a native package. This is
  external recorded RPC over real child pipes, NOT actual Pi acceptance.
- native-first.log:8actual pinned-Pi/localHTTP cases pass41.94s: saved revision
  and credential revision each retire the old child; strict reopen refuses an
  altered identity without sending; explicit new input works after restoring
  this disposable fixture; queued large response/reuse/reopen; cancellation;
  EOF; cold preparation without input/provider call; selected compaction with
  exactly one original admission. Parameterization accounts for the case count.
- native-final.log:5pass25.67s after deleting launch status replicas: three
  affected actual-native custody/preparation/compaction cases plus two MCP
  permission cases with the actual package configured.
- merged-path.log:4pass5.72s on merged main: three declaration/deletion guards
  and the actual selected native compaction path. No repeat of the entire matrix.
- attestation.log:final production2pass7.08s. One ACTUAL native get_state identity
  mismatch refuses input, reaps the retained child and preserves saved bytes;
  the other combines actual strict native disk helper with recorded RPC identity
  refusal. The latter is not mislabeled as actual full native transport.
- declarations.log:59passed and2FAILED due to omitted PI_COMPACTION_TEST_PACKAGE;
  those two exact cases were rerun successfully in native-final.log. Receipt kept.
- acp-caller.log:2FAILED for the same missing harness variable; corrected exact
  two-case caller shard acp-caller-final.log:2pass1.49s. No compatibility restored.
- lint.log:changed production/new affected tests pass; git diff --check passes.

Nine distinct actual backend-native cases across the receipts (eight first plus
one final identity-attestation case), not repeated counts. Canonical native5fde,
local-only config/credentials and localhost providers; disposable saved histories
and bounded subprocess cleanup. No paid providers or installed/live readiness
claim. Parent performs final combined installed acceptance. No unchanged308
output/privacy/retry matrix rerun.

## Architecture and remaining boundaries

The308 full-context source audit is reused as baseline because its production
source equals this branch's f757627a base. Initial custody rescan remains22 raw
findings. Published scan onfc73cb8d plus merged306/307 source reports21 findings
(20semantic_mirror_without_descent,1repeated_builder_calls); this aggregate change
includes parent work and is not attributed solely to311. No new ownership finding
points to backend/native_attestation. No mapping_read/unmodeled_record_shape/
redundant_type_check emitted. CLI omits scan_status and analyzed/omitted detector
counts; no zero-debt or complete detector-coverage claim. Raw JSONs and authored
migration recipe remain under .artifacts/native-custody. No NRA replay-equivalence
claim; behavior evidence is separately listed above.

Remaining backend input scheduling, task cleanup indexes, discovery and tool
presentation are not claimed closed. This slice completes native launch,
attestation and retained-child custody ownership without moving runtime dispatch.
