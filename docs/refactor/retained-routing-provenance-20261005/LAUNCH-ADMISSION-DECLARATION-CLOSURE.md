# Separate original launch and admission closure

This source trace does not qualify a live central batch or hold the stopped
mounted routing proposal. No production changes or runtime operations were made.
The original source is Git72062939239b0f07707406309305a056a23f925f;
current consumed source is frozen Coreb97345686f8b498db24069d262f364c5ad1ffb1b.
The existing dependency graph records syntactic import reachability; it does
not establish that the original declarations satisfy the later contracts.

## Original declarations and current consumers

| Original fact | Current owner and consumer | Remaining relation |
| --- | --- | --- |
| No owner_launch module | owner_restart imports RestartEnvironment and RetainedOwnerLaunch at module load | phased_owner_kernel only loads owner_restart and owner_cutover; the original package cannot resolve the eager launch import. |
| thread_identity has ThreadIncarnation and OwnerIdentity, no AdmissionIdentity | owner_restart imports AdmissionIdentity; RetiredOwnerLaunch stores it | Adding a launch module would still leave an unresolved original declaration. |
| RegistrySnapshot has owner_identity and require_owner_process, and admission_generations, but no admission_identity | FencedOwnerBatch.complete captures admission_identity; RetiredOwnerLaunch.require_current checks it | The old generation facts exist. Their absence as a later typed API is not permission to rewrite the registry or invent an alias. |
| Thread has no execution capability | OwnerRestartRequest.threads selects restart candidates through execution; AdmittedOwnerBatch.restart requires execution.require_native | Explicit names bypass only the selection traversal, not the admission consumer. |
| RegistryDocument has singular fence_idle_owner, no fence_idle_owners | AdmittedOwnerBatch.fence calls the plural batch operation | A sequence of later operations cannot be assumed equivalent to original atomic admission custody. |
| OwnerLifecycle.restart_owners has no cutover argument | restart_original_thread_format and current OwnerCutover.restart require the acquired phase relation | The authentic original lifecycle must own admission, retirement and the handoff; loading current phases alone does not extend that original method contract. |

These sites were read from Git source, not imported under the old interpreter.
No original fields were removed and no original registry document was decoded
through the target's live Thread declaration.

## Handoff, launch, failure and restoration consumers

OwnerRestartRequest owns selection, expected selection and restart environment.
AdmittedOwnerBatch acquires the original wire lock, checks maintenance, role,
active status, process, idle state and exact admission generation, and captures
RetainedOwnerLaunch before fencing. RetainedOwnerLaunch captures the selected
process/interpreter/environment and verifies the original owner identity and
process before and after capture. This is genuine launch custody, not a PID-only
record or a destination snapshot substitute.

FencedOwnerBatch stops the exact admitted processes, verifies retirement under
the original wire lock, captures owner and admission identities, and transfers
the same opened resource and launch set to StoppedOwnerBatch. Its inherited-FD
acceptance validates fstat, named lock inode/device, owner UID, flock and root.
Those checks do not themselves establish the missing original admission APIs.

OwnerRestartHandoff validates the complete retired owner set before any target
launch. RetiredOwnerLaunch.require_current checks owner, admission, exact local
process, idle/stopped state and absence of the retired process. Both target
launch and original restore use that same validation. Original restore also
requires the acquired original interpreter; source environment and arguments
are retained unchanged. Every missing declaration above therefore affects
failure/recovery and original restoration as well as the forward path.

StoppedOwnerBatch.complete transfers the same custody on failure through
StoppedOwnerFailure. OwnerCutover.recover refuses absent an unchanged-original
certificate. restore_unchanged retains the original error, records a refused
recovery and leaves owners stopped through explicit abandonment; it neither
repeats installation nor replays input. PreserveOwnerRuntime can restore only
the unchanged original handoff. Routing after_stopped/quiet installation is a
different, accepted stopped fixture seam and does not claim this live batch.

The next implementation proposal must close this entire acquisition and
declaration relation through the existing original lifecycle and restart
owners. No module copying, source overlay, compatibility property/alias,
registry rewrite, second stop/start authority or fabricated stopped audience is
authorized. No central_batch_used assertion is added to the mounted pilot.
