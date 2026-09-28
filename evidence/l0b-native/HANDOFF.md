# L0B native-only execution and selected followup closure

Pascal: PR234, `refactor/s10-pi-boundary-20260928`, persistent
`~/wt/comms-s10-pi-boundary-20260928`. Parent owns whole-step quiet activation.
No live changes, provider account calls, model workers, or new package installs.
NRA skill and full L0B audit sections 1–4 read. Prior global scan/raw-record
coverage remains in that audit; no new clean-scan claim.

## Implemented

- Removed `backend.rpc_args_for`, raw task-as-argv/stdout execution, text/image
  branching and all production imports. `TurnSession` requires the existing
  `NativePiRpcLaunch`; configured launch validates the exact installation,
  package, RPC mode and sanitized native environment. Model/settings retained.
- Removed `Participant`, `ParticipantEventConsumer`, `agent_loop.py` and the
  independent ACK/poll scheduler. `agent-comms-agent` uses canonical `worker.main`.
  Existing owner identity/project preserved; fresh identities use invoking cwd.
- Removed `native_session_reopen.package_for_launcher`; reopen takes the verified
  package. Actual installed launcher, symlink, pinned CLI and refusal scenarios
  now exercise `NativePiRpcLaunch.package_for_command`, with no alias.
- S10 startup metadata accepts stock Pi UUID parents while retaining strict
  startup entry ID and exact fresh parent/input matching (7cd8702).
- SelectedExecution now participates in existing TurnRunner locks/tasks and
  InputDrain backend inbox. ACP fresh owner text is accepted immediately; the
  same live inbox enters native execution after selected settlement. No second
  queue, direct_interrupt ticket, durable UNKNOWN reconstruction or replay.
- Fresh input preserves its original public ID/native-start receipt, images,
  permission controller and compaction future-input receipts across the lease
  handoff. Pipe-write admission rechecks exact goal/wait, owner and input key.
- Original owner inputs do not borrow a parked goal grant, consume its wait, or
  block it when unrelated native work fails. Autonomous goal/dependency work
  retains its real permit and existing settlement rules. Narrow ownership was
  announced on Nietzsche PR239 and parent229; no goal store/schema edits.
- ACP stdio passes its already-validated private package/root to CommsClient.
- Removed obsolete direct_interrupt test driver; current selected-native tests
  retain its behavior obligations using current registry/SQL/ACP and real Pi.

## Focused receipts

- `binding-fixed.log`: 73 native binding/nominal cases pass after migrating the
  steering environment consumer to launch.env.
- `launcher-callers.log`: four current launcher-resolution scenarios pass.
- `native-headless-current.log`: five cases pass, including actual prepared Pi
  GetState and canonical headless process/socket attach/relay/owner-project.
- `current-stdio.log`: actual stdio initialize/new/prompt and separate owner pass.
- `selected-native-current.log`: 19 cases pass, including two real pinned native
  children, one selected DM and one fresh followup, deterministic loopback only.
- `actual-native-parked-followup.log`: same actual path with a parked goal/wait
  retained, exactly two requests, fresh input STARTED once, no goal store created.
- `selected-input-retention-repair.log`: images/controller/two queued receipts,
  compaction row eligibility and duplicate-start refusal pass.
- `current-selected-fixtures.log`: 58 pass, one stale golden-field assertion;
  fixed to current declaration names, the exact assertion passes in the later
  goal seam run. Native selected failures/races and original callbacks retained.
- `goal-seam-current.log`: 37 pass, eight fixture failures lacking canonical
  markers. These are already corrected in S13; integrating that source next.

Failed receipts are retained. No repeated provider proof or full-CI gate.

## Coupled scope

S9 a346c9f manual-compaction argument/adaptive caller migration is integrated.
Darwin owns the remaining manual_compaction_bridge basename-gated writer deletion;
requested exact current bridge policy on PR236, no compatibility helper restored.
Parent owns current queue-restored presentation deletion and paired Toad rollout.
Current cursor fixture follows already-emitted S12 `owner_admission_generation`,
`assignment_id` and `kind=current_native_cursor`; no runtime format shim.

Final package/build and combined local caller check follow dependency integration;
this receipt does not claim installation or full branch readiness yet.
