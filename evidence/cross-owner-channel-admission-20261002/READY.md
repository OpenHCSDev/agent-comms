# #524 — source ready for paired installation

Production: `ee84a488410459e4586ded1dbbabcbae76e90025`, original source base
`48d408a4`. 10 production files: **39 lines deleted, 64 added**. Final evidence
and fixture commit changes no production source, native pins, schemas or wire formats.

## What changed

Native prompt admission incorrectly borrowed response publication's wire/bus/
registry exclusions even though it appends no bus row. Actual original #openhcs
358 receipts show pre-byte admission waits of 92.145s and 122.633s; pr159's full
input waited 81.815s. Provider requests themselves overlapped. The original six
replies took 255.582s; all 30 peer claims closed at 340.105s. These measurements
are preserved, not rerun or attributed wholly to one raced lock observation.

`MaintenanceBarrier` now owns shared sync/async ingress. Private admission retains
original SQL exclusive commit, registry shared custody, exact source/prompt/native
journal verification, one-use token and UNKNOWN checkpoint. It closes all resources
before any prompt byte. Ordinary native writes keep shared maintenance custody
through the synchronous final write and release before drain. Stop, rename, turn
lease changes and maintenance remain exclusive. Response publication retains bus/
registry exclusive locks and original SQL/dispatch receipts, with shared wire
custody. Native get_state, phase/activity and display scope reads use shared custody.
No new queue, registry, cache, store, phase mirror or provider retry was introduced.

Ownership patterns: IMPL-13 (existing lifetime owner across drivers), IDEN-7
(publication exclusion borrowed for non-publication), replaced lifetime decisions
removed in place; no alternate gate. Original durable sources and uncertain attempts stay
unchanged.

## Whole source pass

`before-lock-consumers.json` and `after-lock-consumers.json` use the existing NRA
refactor-audit Package parser at exact base/candidate revisions: **311 parsed
modules, zero omissions** each. They enumerate every lexical physical lock scope
(136 before / 135 after), context suspension, and 28/31 targeted caller leads.
AST does not prove dynamic resolution; the following lifetimes were read in source.

* All global-wire scopes and wrapper callers were followed, including paths not
  named `_wire_lock_path`: CollaborationLedger, Registration, SelectedSummaries,
  WireLog context/retained inspection, owner restart and CompactionBoundary.
* Shared ingress callers: backend initial input and TurnInputs forwarding close
  before stdin drain; TrackedTurnSession get_state/attest sends close before drain
  and next_event; OwnedSendAdmission yields only final synchronous writes.
  PrivateSendAdmission commits/closes SQL, raw journal and registry before yielding
  the raw byte writer. No provider await retains these resources.
* NativeSessionPreparation shares TurnSession's actual launch/get_state transport:
  per-session writer/persistent locks and four startup slots, no wire EX. Launch
  attestation releases startup before input. StatsRequest get_state/session stats
  and selected Pi settings/dry-run observations await only original per-session
  transport resources. Native preparation/context assembly runs outside response
  publication scopes. Fresh selected session enrollment is local create/fsync/SQL
  under the shared response boundary, never a provider turn.
* Post-output SelectedAttempt.run finishes native custody before action apply,
  context commit, SQL finality and response publication. LiveResponseOwner runs
  the original sync prepare/append/settle functions in Coordination.run_async;
  their shared wire boundary retains exclusive bus/registry only for local durable
  dispatch/append/receipt work. SourceCoverage reads native proof before the cursor
  commit boundary. Cursor recovery, private stage failure, and attempt recovery use
  this same boundary; recovery's yielded proof is consumed by synchronous settlement,
  not a new provider/input. Goal wait consumption is a separate local durable commit.
* Remaining exclusive wire scopes are original local cross-store mutations or
  read snapshots: registry/goal/queue transitions, catalog/relationships, human
  publication/read ACK, claim publication/release, actual selected file writes,
  and operator lifecycle. InputDrain/OwnedTurn/AcpEventConsumer queue settlement
  closes before emitting events or awaiting another native turn. Read/export
  snapshots close wire custody before record traversal/export/render work.
* `_selected_claim_boundary` has exactly two callers: synchronous original claim
  publication and generation-bound release; no await/provider call. Selected file
  mutation holds original custody through bounded file content/fsync, not tool/model
  work. Selected wake verification is synchronous certified source/SQL reading.
* OwnerLifecycle signal guards yield only exact guarded signal syscalls; the
  ChildProcess stop plan closes each guard before yielding its sleep/exit wait.
  RetainedOwnerLaunch captures original process/environment facts, no provider.
  Foreground registration/retirement closes before actual SelectedExecution awaits.
* **Notable retained local child waits under EX**: CompactionBoundary hands original
  descriptor custody to NativeCompactionWriter for bounded local commit IPC
  (validated timeout up to 30s, default commit budget 5s); its provider summary
  await is outside hold. Arendt owns this coupled native commit lifecycle.
  ThreadManagement.fork runs the existing SessionManager fork helper under EX
  (PiHelper's 10s local child bound, offline/no prompt); original fork facts remain
  atomic. These are not claimed removed, measured during seq358, or provider waits.
  Arbitrary SQL/fsync/OS latency is not claimed bounded by this lexical source pass.

## Final checks and limits

* `installed-parallel04.log`: **1 passed in 19.93s**. Existing actual native/channel
  driver, three real separately owned workers, installed wheel, ACP observer,
  localhost provider. Endpoint phase comes from original NativeRuntimeInput.stage,
  joined to the actual saved tracked user input ID. Reply eligibility comes from
  original assignment references. No retained-text phase detector or new counter.
* Both original triage provider requests overlapped: the first response stayed
  withheld until the second arrived (0.621s apart). Two full native answers
  published exactly once, all six original/peer claims settled, seven actual
  native requests, three saved native sessions. No new native request during the
  final stationary guard. All original fixture owners stopped and exact birth
  witnesses are now absent; localhost serving thread retired.
* `final-exclusion-controls.log`: **7 passed / 1 fixture failure in 3.86s**. Passed
  controls exercise original SQL-reader contention/commit before bytes, one-use
  token, revocation with zero prompt bytes, release before raw writing, and
  exclusive maintenance waiting through final shared write. The ordinary test
  fails before the changed gate: old fixture calls attach_session("project",...)
  where the existing current API requires original Thread. No production adapter
  or compatibility reader was added. Older fake drift controls also have obsolete
  helper signatures; they are not represented as production passes.
* Earlier parallel01/02 failures are retained: endpoint proposed obsolete plain
  text / a triage verdict as full output. Production rejected them and retained
  diagnostics. Parallel03 proved concurrency but still selected phase by text;
  parallel04 replaces that decision with original native identity/stage.
* `installed-source-proof.json`: all **339 packaged source/resource files equal**
  the reviewed source; normal wheel install/direct URL, SDK0.12.1, pip check passes.
  Native5184 unchanged and verified through the actual installed native path.

**This is not final user acceptance or a proven public one-minute latency result.**
Parent installs the paired reviewed source and performs the same actual configured
#openhcs sender→all capable replies→peer ACK journey. Original blocked/UNKNOWN
inputs remain preserved; no repeated public probe or replay was made here.

Raw private originals, native journals/input proofs and negative receipts remain:
`~/.cache/agent-scratch/comms-cross-owner524-20261002/installed-parallel01` through
`installed-parallel04`. Candidate wheel/prefix remain in this owned WT's `.artifacts`.
No public processes, configuration, packages or root data changed.
