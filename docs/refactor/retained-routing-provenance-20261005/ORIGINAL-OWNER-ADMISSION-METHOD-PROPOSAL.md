# Original owner admission and launch implementation proposal

Historical initial method proposal below is preserved. The implemented
successor is documented in CURRENT3931-PHASE-TRANSPORT-RELATION.md: the original
owner/admission/launch handoff is canonical; stopped capture/validation moved
from live selection to RetiredOwnerLaunch. P now declares AdmissionIdentity from
its original allocations. These supersede the initial selection-wire choice
below. No installed central batch is claimed.
Einstein owns this closure. The existing stopped-routing tuple b52ed4da/145cd9b5
and its selected b973/d08 source and c44/0fb wheels remain unchanged and unissued.
This proposal is on a separate working branch based on 8877cab7; it does not
adopt Parent688 or relabel either frozen wheel as current main.

## Decision and genuine producer

Change the original lifecycle and registration methods in a versioned source
successor of genuine Git72062939239b0f07707406309305a056a23f925f. Call its eventual
commit P, not 720. P retains the original Thread/RegistryDocument wire
declarations, including last_goal_report_turn, original FieldCodec, process
identity and existing generation counters. Its method changes and normal build
inputs must be recorded exactly. A wheel built from P would be a newly built
**amended original-format producer**. It would not be the c3e exact720 wheel,
an unchanged720 process, or an original historical installation.

The current target restart family gets the same owner-method contract. Normal
source installation supplies the complete reviewed family before execution.
Delete the dynamic two-module phase injection; do not add more injected modules,
imports, AdmissionIdentity aliases, Thread.execution properties or missing-field
fallbacks. The source lifecycle selects and retires its own declarations. Only
its acquired launch/identity handoff crosses the process boundary.

This chooses existing owners and shared phases (IMPL-12, IMPL-13, BOUND-2,
IDEN-8). It rejects a compatibility entrypoint (TIME-3) and the new-type/old-shape
adapter (TIME-9). Source and target are explicitly selected producers; neither
discovers a format from absent fields or decodes the other's live registry.

## Exact source methods and deletions

| File and owner | Change | Replaced decision |
| --- | --- | --- |
| Original and current threads.py / Thread | Add restart_candidates(status) and require_restart_owner(status). In P, use its original role, status and process capabilities. In current source, delegate native eligibility to existing ThreadExecution before the same role/status/process requirements. | Selection and native eligibility embedded in OwnerRestartRequest and AdmittedOwnerBatch. No execution field is added to P. |
| Original registration.py / Registration | Replace the per-owner public fence wrapper with the existing complete-batch fence_idle_owners transaction. Reuse original RegistryDocument.fence_idle_owner within one editing scope and commit only after all members pass. | The original lifecycle's separately committed list of fences. RegistryDocument's single-member validation/transition remains the owner; no second generation store or plural document algorithm. |
| Original and current owner_lifecycle.py / OwnerRestartSelection | Add capture_retired(snapshot, original) and require_retired(snapshot) beside existing capture/require_current. Carry existing OwnerIdentity, exact ProcessIdentity and admission_generation. | Post-retirement owner/admission capture and validation scattered across FencedOwnerBatch and RetiredOwnerLaunch. No AdmissionIdentity import or snapshot.admission_identity is needed for this handoff. |
| Original owner_lifecycle.py / OwnerLifecycle.restart_owners | Replace its inline selection/fence/stop/launch body with the same OwnerRestartRequest -> OwnerCutover -> AdmittedOwnerBatch entry. Keep _stop_process, signal guard, voluntary-release receipt and _launch_owner_unlocked as the only signal/launch implementation. | Original inline procedure, not the authentic source decoder or lifecycle authority. |
| owner_restart.py / OwnerRestartRequest | threads(snapshot) delegates to Thread.restart_candidates; explicit names still require active original declarations and deduplicate canonical names. Expected selection uses original OwnerRestartSelection. | Direct access to thread.execution and duplicated selection predicates. |
| owner_restart.py / AdmittedOwnerBatch | require_restart_owner, exact selection/process/environment capture, then atomic Registration.fence_idle_owners. | Native type knowledge in the batch; no weaker explicit-name path. |
| owner_restart.py / RetiredOwnerLaunch | Store selection: OwnerRestartSelection plus launch: RetainedOwnerLaunch. name derives from selection; require_current calls selection.require_retired and checks launch.process equals selection.process. | Separate owner/admission fields and their later AdmissionIdentity/snapshot API dependency. AdmissionIdentity used elsewhere in current live turns is not removed or aliased. |
| owner_launch.py / RetainedOwnerLaunch and RestartEnvironment | Implement the actual source capture/environment contract against P's original ProcessIdentity/Platform, owner_identity/require_owner_process, FieldCodec and PathText. Preserve source shell arguments, credentials/config and interpreter checks. | Operator-environment substitution or bare-PID capture; no wholesale module transplant at bootstrap. |
| owner_cutover.py / OwnerCutover, StoppedOwnerInstallation, PreserveOwnerRuntime | Keep their current operation and failure/recovery ownership, supplied as normal reviewed source in P. Source lifecycle invokes this callback only after complete retirement with the same wire resource. | A second stopped callback or outside stop/start mechanism. |
| phased_owner_kernel.py, restart_original_thread_format.py, restore_stopped_owners.py, cutover_child.py | Remove spec_from_file_location/sys.modules injection and source-package-path arguments. Source restart and recovery import P's own installed phases; handoff-selected original interpreter/environment remains authority. | Loading selected target methods into an unchanged old package and repeating that bootstrap during recovery. |

The new phase declarations needed by P are authored as part of that explicit
source amendment, with the full original dependency contract reviewed. Adding
owner_launch alone is not the change. P also needs the Thread methods,
Registration batch transaction, retired selection methods and lifecycle
callback. No changed producer is concealed behind unchanged720 provenance.

## Phase contract

### 1. Select and admit under the original wire lock

OwnerLifecycle owns the root and authentic Registration/MessageBus. Maintenance
must be open. OwnerRestartRequest derives the complete managed audience from
the original Thread capability; an explicit subset remains subject to the
operation's complete-audience check. Preserve the original expected selection,
role, active status, exact living process other than the caller, and idle-turn
refusals. Do not substitute the target's selection or a dead historical PID.

Capture each original owner and admission generation from one original snapshot.
RetainedOwnerLaunch then validates process birth before/after /proc acquisition,
original launch-name aliases, owner identity, original interpreter and shell
arguments. OwnerRestartRequest.environment supplies only declared target runtime
fields; each acquired source launch retains its own credentials, settings,
arguments and configuration. No environment or credential is saved to a file,
argv or persistent handoff record.

### 2. Fence all, then stop exact processes

Registration.fence_idle_owners uses one original store editing/commit scope;
RegistryDocument.fence_idle_owner remains the per-member owner. A busy, replaced,
renamed or changed-generation member refuses the entire transaction. No signals
occur before all fences commit. The current implementation already owns this
batch transaction; it is the original Registration public method that lacks it.
There is no need for a second plural algorithm on RegistryDocument.

FencedOwnerBatch uses original OwnerLifecycle._stop_process and its exact-birth
signal guard. Preserve _require_same_stop_owner and the original durable
voluntary-release receipt; a generation advance alone is not OS retirement.
Partial retirement never authorizes quiet installation or replacements. The
same captured audience must be joined and checked before the stopped phase.

### 3. Acquire the stopped witness and transfer the same resource

After all exits, capture_retired reads the original post-retirement snapshot.
It requires the same incarnation/process, idle state, stopped status and absent
original process, and records the actual resulting owner/admission generations.
It does not reuse a pre-fence generation or call the active require_current on
a stopped thread. require_retired owns the identical checks at handoff use.

StoppedOwnerBatch retains the original opened wire StoreLock and ExitStack.
RetainedIndexCutover.after_stopped -> quiet_install remains the one original
writer/index operation. The writer opens bus through its original owner; the
target installer inherits that original integer OFD and verifies fstat/flock/
root, as in the accepted stopped seam. No operation reopens wire or reacquires
the inherited bus lock by path. Stop, quiet write and launch are not recoded in
the tool or installed pilot.

The handoff contains root, bounded retired selections, exact launch captures and
declared target runtime policy, never live Thread or RegistryDocument records.
Transfer only through the existing joined subprocess stdin and inherited FDs.
Target FieldCodec decodes that explicit handoff declaration once; it does not
decode old source Thread fields. Original registry fields and historical source,
bus and guard remain under their original declaration/lock owners. Legitimate
original fence/release transitions are accounted runtime changes, not a format
rewrite or permission to strip retired fields.

### 4. Target launch has a strict, separate registry precondition

StoppedOwnerBatch.accept keeps exact UID/device/inode/regular-file/flock/root
checks. OwnerRestartHandoff validates the entire retired selection set against
the target's **already legitimately installed current registry** before the
first route publication or launch. It preserves alias/incarnation, owner and
admission generation, exact retired process, stopped/idle state and no surviving
source process. Target launch uses only the target OwnerLifecycle's original
launch implementation and declared private root/native binding.

Quiet routing/index installation does not convert the old Thread registry.
Therefore this source-method amendment alone cannot launch current owners from
a still-old720 registry. The target must refuse it. It must not create Comms on
old source bytes merely to inspect a witness or use recorded RegistryProvenance
as a live owner. HistorySource's recorded projection is a reader authority, not
an executable owner catalog.

The existing separate thread-format retirement family is the owner of a genuine
format transition; this proposal neither runs it, adds field stripping to the
routing operation nor assumes its post-image exists. A future combined live
qualification must explicitly bind that owner's complete target-declaration
post-image and preserved original preimages before target launch, or qualify
only a same-format amended-producer restart. An old-format target refusal is
part of this contract, not a request for another global audit or a hold on the
stopped routing App. No target launch or full centralbatch credit is inferred.

### 5. Failure and unchanged-source recovery

Before fences, refusal changes no owner or files. After partial retirement,
preserve the exact captured/fenced state and original error; no replacement or
quiet operation starts. After complete retirement, StoppedOwnerFailure transfers
the same handoff and wire custody. The operation alone certifies unchanged
original durable data. RetainedIndexCutover and routing remain leave_stopped on
a possibly changed index; unchanged recovery is not inferred from an exception.

For a certified unchanged operation, source recovery invokes the acquired P
interpreter using its retained source environment and its installed phases,
with the same inherited wire FD. It rechecks every retired selection before
any source launch. No target live decoder, kernel module injection, repeat
fence/seed/install/input or automatic source rollback is allowed. A refused
recovery retains both errors and explicitly leaves owners stopped. Credentials
remain in RAM for the handoff lifetime and are disposed when custody closes.

## Complete consumer migration

Keep OwnerRestartRequest.threads(snapshot) and OwnerCutover.require_selection
signatures stable through the Thread-owned methods. This migrates the managed
audience used by RetainedIndexCutover, ThreadRetirementCutover,
RetainedTaskSourceCarry, publish_retained_summary and publish_openhcs_recovery
without copying selection logic. Global extension installation uses the same
selection callback and remains an operation, not an owner selector. Keep all
their failure policies and recovery_paths; frozen issued operator files are not
edited or rebound by this source proposal.

Both the old CLI restart command and restart_queue use the amended original
OwnerLifecycle entry. Preserve their existing explicit argument/default
semantics when replacing the inline body. Original single Registration fence
consumers migrate to the atomic batch method; RegistryDocument's single-owner
method remains required inside the transaction. Current ordinary restart/start/
stop callers retain their independent legitimate behavior.

Migration of RetiredOwnerLaunch's declared wire shape must include current
OwnerRestartHandoff decode/launch/restore, launch_thread_retirement,
restore_stopped_owners, cutover_child.restore_stopped_batch, original bootstrap
arguments and source/target controls in the same source change. No alias for the
old handoff shape. Existing historical receipt bytes stay immutable; an issued
old operator tuple never silently adopts the new shape or helper.

## Evidence, collision and final controls

Reuse LAUNCH-ADMISSION-SOURCE-DEPENDENCIES.json: current324 and original286 modules
parsed with zero omissions; 309 syntactically reachable modules, 39 absent in
original720. Deferred/TYPE_CHECKING edges and dynamic resolution are not runtime
proof. The semantic sites above close the actual selection, capture, admission,
handoff, forward launch and recovery decisions rather than treating module
presence as a capability. No new scanner or repeated whole audit is needed.

Current owner claims checked: Core68719bb and682c81 production edits are only
session_lifecycle.py. Parent688 is integration, not a new method writer. Einstein
proposes the restart/Thread/Registration changes above; any new collision on
those precise methods is coordinated before production writes. No source is
written in Parent, Mendel, Heis or another worktree.

After coherent implementation, update the existing owner-process and
registration-transition controls in one affected batch: original full-audience
selection, busy/changed-second-member no partial fence, exact process/environment
capture, voluntary release, complete retirement, same wire+bus OFD acceptance,
wrong-root/descriptor refusal, target-old-registry refusal, complete compatible
target launch, and certified unchanged-source recovery/refused recovery. Reuse
their original process and store owners; no mock alternate lifecycle or new
control framework. Native/runtime tests are not run under this source task.

Before any future build, identify P's exact source amendment, declared resource
pin, dependency closure and full normal wheel inputs. The original720 c3e and
built routing c44/0fb remain distinct artifacts. No matching native artifact is
inferred from old resource4ab or from current4b availability. Any future live
purpose must bind its genuine producer/target declarations and original native
authority separately. No build, package, original data/root, native, provider or
installed operation is authorized by this proposal. central_batch_used remains
false for every existing stopped routing acceptance.
