# Retained batch maintenance cutover

Arendt owns the existing OwnerLifecycle.restart_owners stop-all to launch-all boundary, paired with Mendel430's disposable index rebuild owner. Current435 has no operation between those phases. A new private wire pin validates the current index schema before acquiring a batch, so an operator cannot construct a new-runtime private pin against an old index and then rebuild it afterward.

Extend the existing retained batch custody with one explicit maintenance operation carrying the original writer lock/certificate. Preflight and capture every exact idle original owner before the first stop; execute maintenance only after all selected original processes exit and before any replacement launches. Retain each original launch setting in existing RetainedOwnerLaunch. No operator stop/start loop, second registry, guessed process proof, live original replay or automatic retry after partial failure.

Mendel owns index format, rebuild and writer certificate semantics. Parent owns eventual global activation after Sch215's real gate. This draft is source ownership and implementation scope, not cutover authorization or proof that the live installation changed. Resource436 remains independent.

## Typed operation and verified boundary

`OwnerCutover` declares preflight selection and the all-stopped operation;
`PreserveOwnerRuntime` retains normal batch behavior. `restart_owners` is the
only stop/start path. No public raw callable, second launch queue or copied
owner registry was added. Original exact-process/per-owner launch capture,
whole-batch idle fence and post-stop revalidation remain in that owner.

The one-shot `tools/cutover/RetainedIndexCutover` member requires every live
executable owner and checks the two installed writers' declared schemas differ
before any fence or signal. Schema objects are derived from each installed
`CheckpointTable`, not from a second catalog or a guessed format number.
After all original processes exit, under the batch's original wire lock, the
authentic old interpreter obtains `Comms.bus.log.locked()`. Its old certificate
is validated before mutation. It changes only `checkpoint_version` and
`checkpoint_seal` to None, removes the derived SQLite index and launches the
new installer with `pass_fds=(custody,)`. The child verifies the original lock
inode/open description and installs with `_bus_locked=True`. It never reenters
the old writer or wire lock. The old parent remains alive through the child,
then verifies original bus SHA and every other marker field are unchanged.
Only after successful installation does the existing lifecycle pin the new
native runtime and resume its retained batch. A failed operation leaves the
batch stopped; no automatic retry or replacement launch hides uncertainty.

Actual installed old720 → new schema gate EXIT0: new writer initially refused
the old index, inherited original writer custody was verified, original bytes
and frozen sender/recipient identities survived rename and membership change.
This isolated boundary gate had zero surviving owners, native inputs, replay
or provider calls. [Receipt](../../evidence/retained-index-cutover/old720.json).

The existing actual two-worker installed batch gate passed in36.22s. It covers
different arguments/credentials, rename, one busy owner refusing all stops,
incomplete audience refusing before stops, all originals exiting before the
declared writer operation, exact retained registry/process revalidation and
both replacements attaching. It now waits for the existing protocol's complete
`ready` response: socket existence alone raced startup history replay in the
failed04 trace. All fixture workers are retired. This gate rebuilt its own
current-format fixture index; it is separate from the authentic incompatible
old720 writer boundary above. Neither gate changed the live root.

## Operator use after the parent's paired gates

Run from the reviewed new Python, with its target runtime environment, and add
this worktree's `tools/cutover` to Python's module path. Construct `Comms(root)`
directly, without trying to create a new private pin against the old index.
Then make ONE call:

```python
service.owners.restart_owners(
    source_interpreter=str(old_python),
    runtime=RestartEnvironment.inherit(os.environ),
    cutover=RetainedIndexCutover(old_python, original_root_id, reviewed_native_package),
)
```

Parent owns pausing external ingress, exact selected old/new installations,
global gate approval and activation. The original root/package values are
explicit reviewed proof, not guesses. The operation must not be rerun after a
partial failure without reviewing original durable state. Remove the one-shot
tools after the supervised transition; no old reader remains in production.
