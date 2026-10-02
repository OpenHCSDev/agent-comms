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
  markers. Integrated S13 e96a997, preserved Participant deletion on conflict:
  `goal-seam-integrated.log` **53 pass**, including the corrected cursor assertion.
- `current-guard-collection.log`: all 2,999 current tests collect, 23 guards selected;
  `current-refactor-guards.log`: **23 pass**, no exceptions.
- `build.log`: current wheel built. `installed-native-headless-stdio.log`: **6 pass**
  importing the extracted wheel (including child subprocess imports), real native
  GetState, native refusal, canonical headless socket/project and ACP stdio.
  Artifact inspection confirms agent_loop absent and headless entrypoint worker.main.

Failed receipts are retained. No repeated provider proof or full-CI gate.

## Coupled scope

S9 a346c9f manual-compaction argument/adaptive caller migration is integrated.
Darwin owns the remaining manual_compaction_bridge basename-gated writer deletion;
requested exact current bridge policy on PR236, no compatibility helper restored.
Parent owns current queue-restored presentation deletion and paired Toad rollout.
Current cursor fixture follows already-emitted S12 `owner_admission_generation`,
`assignment_id` and `kind=current_native_cursor`; no runtime format shim.

## Source publication and remaining boundary

Stable production source: `16053b4` (substantive native followup `f81ac72`).
PR234 contains all current code and consumer/test changes; the final documentation
commit adds no production changes. Wheel:
`.artifacts/wheels/agent_comms-0.1.0-py3-none-any.whl` in this persistent tree.

Own L0B production edits (7cd8702, e13e32e, f81ac72): **453 added / 492 deleted**.
These counts exclude integrated parent/S12/S13/S9 edits, tests and evidence.
The followup fix adds required behavior that was absent from selected execution;
the replaced text/poll/classifier implementations and their consumers are deleted.
Original broader S10 deletion receipts remain in evidence/s10-pi-boundary.

No source blocker remains in the native launcher/headless/fresh-input implementation.
The coupled S9 manual compaction bridge remains Darwin's explicit scope, not closed
by these tests: native-only managed owners must use his journal/CAS path, not the
remaining basename-gated alternate writer. Parent owns its integration and quiet
whole-step install. This handoff does not claim live deployment or paid-provider proof.

Owned wheel extraction was removed after all child processes exited; wheel and all
success/failed receipts retained. No copied native bundle or extra environment.
