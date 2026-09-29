# S2 progress watchdog — PR303

Code checkpoint53767f46, based on merged03b6f9f4. Parent owns review,
integration and live deployment. No live owners, Toad, native package, schema,
provider credentials, or stored history changed.

## Completed ownership and deletion

S2's planned ProgressWatchdog now owns progress timestamps, startup measurement,
preflight and input-start deadlines, phase, retry recovery, and observed output,
tool and compaction safety flags. It schedules the cancellable RPC read race,
reacts to real event capabilities and owns timeout/state diagnostics. The
watchdog reads actual input and stats authorities; it cannot admit or replay an
input. PiEvent, PiCommand, PiPayload and StatsRequest consumers use that owner.

TurnSession.read_rpc_line, turn_state and handle_timeout are deleted, along with
their session-owned fields and deadline branches. No forwarding methods or
aliases remain. Existing PiRpcChannel, TurnPhase, InputForwarding, StatsRequest
and TurnFailure remain the authorities for their respective behavior. The final
clock correction keeps compaction's input-start deadline relative to the original
event timestamp, including if emitting usage paused the consumer.

Backend1409→1244lines. Production322added255deleted (net67): explicit watchdog
construction and behavior replace dispersed initialization and scheduling; the
new component owns state rather than forwarding to session fields. Tests200added,
8deleted. New watchdog236lines, longest method60lines. The existing deletion
guard was extended only for the three newly retired methods.

This closes the watchdog slice, not all S2/S7. Backend still owns native launch,
attestation, identity invalidation, retention, and terminal outcome construction.
Dalton owns legality-rule/S3 acceptance; Boyle owns MutationStore.

## Evidence, kept at its actual strength

- `backend-corrected.log`:196 recorded-child backend/settlement checks pass29.46s.
  Initial194pass2fail is retained; a relocated stats request received the watchdog
  instead of the actual turn and was corrected.
- `native-complete.log`:7 actual pinned-native/localHTTP matrix checks pass21.82s.
- `installed-native.log`:11 actual-native tests pass43.90s against the separately
  installed7c8765ff wheel: matrix7, ordinary lifecycle3 (2MiB output, reuse,
  validated reopen, cancellation and EOF), canonical selected summary→native
  commit→exactly one original admission1.
- `final-compaction-installed.log`:final53767f46 wheel5pass16.57s. Rechecks the
  four native excursion outcomes and canonical selected compaction after the
  event-timestamp correction. Unchanged native failure/stall/steering/lifecycle
  cases are not claimed rerun in this last narrow shard.
- `baseline-native-corrected.log`:base03b6f9f4 passes the same7 matrix cases23.46s.
  `native-equivalence.json`:six emitted AgentEvent receipts match installed
  candidate exactly after normalizing only isolated session path and elapsed_ms.
  Steering satisfies the same actual assertions in both runs. This compares
  decoded event receipts from real executions, not raw RPC transcript replay.
- `payload-and-deletion.log`:18 typed-payload/new deletion checks pass0.22s.
- `lint.log`:changed-file Ruff passes; git diff --check passes.

### Native matrix meaning

Managed pinned CLI: provider503 failure, no-progress watchdog abort, explicit
steering interrupt. Each preserves the observed input count; no automatic replay.

Native SDK external-protocol oracle: provider retry recovery, overflow compaction
success, compaction failure, compaction abort. The existing native host fixture
now accepts explicit settings and uses its selected provider. Its test-only
UntrackedOracleTurn sends Pi's untracked prompt form. Managed tracked inputs
intentionally disable automatic retry/compaction; these oracle cases are not
native input-admission proof and do not change managed production policy.

The pinned package also explicitly disables summary retries inside default
compaction. The current failure contract is one failed summary HTTP call, no
second summary request, and no committed compaction. The original S2 plan's
summarization-retry live case cannot be emitted by this pin. The event declaration
remains an external protocol contract; existing backend replay coverage exercises
it. We did not alter Pi or inject events to manufacture a native retry receipt.

### Failed attempts retained

- `native-first.log`:two too-short350ms watchdog deadlines elapsed before native
  input-start observation; steering passed. Corrected test budget2s, same input
  and no-replay assertions.
- `excursions-first.log`:attempted automatic excursions with tracked inputs;
  current Pi correctly disabled both. Separate untracked native oracle added.
- `excursions-rest.log`:obsolete summary-retry expectation; compaction abort
  passed. Current single-summary contract was verified in native source and
  asserted explicitly, including no retry event.
- `baseline-native.log`:baseline extraction omitted stack manifest/resources;
  all7 refused before launch. Corrected extraction includes base stack files.

## Architecture audit

Full src/agent_comms scans with src dependency context before/after the code
batch:22 findings unchanged (21semantic_mirror_without_descent,
1repeated_builder_calls),35.315s/32.074s. No mapping_read,
unmodeled_record_shape or redundant_type_check was emitted. The tool emits no
scan_status or analyzed/omitted detector counts; absence is not proof of complete
detector coverage or zero debt. Scans preceded the final small compaction-clock
helper; no global zero-debt or whole-plan completion claim. Raw JSON stays in
owned .artifacts/watchdog; compact counts/timing receipts are committed.

## Candidate and cleanup

Final noneditable wheel and install:
`.artifacts/watchdog/final-wheel/agent_comms-0.1.0-py3-none-any.whl`
`.artifacts/watchdog/final-installed`

No live activation performed. Local tests suffice; CI is deferred. All native
and loopback children were bounded and closed by their fixture cleanup. Only
owned disposable baseline/test/cache directories are removed after exit; source,
branches, immutable native bundle, wheels, receipts and audit JSON are retained.
