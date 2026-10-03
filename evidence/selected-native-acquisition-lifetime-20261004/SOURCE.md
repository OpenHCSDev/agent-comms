# Selected native acquisition lifetime

Mendel owns native_pi.py, coordinated_runtime.py, tracked_turn.py and their
complete launch/evidence consumers. Sch608 owns native_package.py, import fence
and artifact builder; those files/artifacts are untouched. Base is merged605
db620680bf6f52133157209b2a7b557eaef988a1. Existing606/603 integration and
Arendt's exclusive thin540 loan remain independent.

SelectedExecution.validate verifies the package before selecting a claim but
discards the returned acquisition. First tracked_launch independently verifies
the same immutable artifact; only later stages reuse the first actual launch.
NativePiRpcLaunch will own an acquired executable construction factory. One
selected execution retains that original factory from its joined pre-claim
validation through every stage. Standalone tracked calls acquire afresh. Source,
header, model, auth, tool restrictions, input proof and child attestation remain
fresh per stage. Delete the optional acquired_launch/first-launch selection.
No verified flag, path cache, new resource class or second trust store.

TrackedTurnSession.committed_input also reads its acquired evidence descriptor
with unjoined asyncio.to_thread. Cancellation can leave that worker using a
descriptor already closed by the enclosing AsyncExitStack. The same owner's
context_proof already uses Coordination.run_worker, whose original retirement
join waits through cancellation. Migrate the remaining borrowed read there.
No new worker queue or cancellation policy.

Patterns IMPL-12/13 and BOUND-1: consume one acquired resource and one joined
lifetime instead of repeating acquisition or leaving a weaker resource path.
before.json enumerates declarations/imports/decisions/consumers with existing
refactor-audit AST parsing. Omissions and dynamic-resolution limits are explicit.
This is a source deletion, not attribution of the historical13.562/13.696s gaps
or98/99s provider duration. Final checks must detect renewed full-tree acquisition,
stale auth/header reuse, invalid independent artifacts and descriptor retirement
before a cancelled read finishes. Actual installed saved-source/native custody
qualification requires a released existing holder; no new env/native/provider
reproduction or public action.

## Working source

NativePiRpcLaunch.acquire_tracked performs the original _trusted_package call
and returns a partial of that class's complete _tracked construction. The
captured executable factory is assigned only after joined validation succeeds,
before participant/lease selection. SelectedExecution.tracked_launch consumes
it with each original stage's arguments. Standalone tracked acquires its own
factory. Both production and the direct SDK custody control validate before
tracked_launch; there is no unvalidated resource use in the enumerated callers.
Remove _native_launch and acquired_launch entirely, including the obsolete
control consumer. The acquired factory is an executable resource owned by this
one-use execution, not another model/configuration or input authority.

The evidence-reader cancellation change uses the same existing worker as
context_proof; all production asynchronous borrowed input-reader observations
now join. Synchronous observations remain inside their original owned scopes.
Other to_thread sites are not borrowed readers of this descriptor and are not
changed merely because they use the same Python scheduling API.

The updated existing actual SDK custody control requires one verification for
pre-claim plus both stages, current auth on the next stage, distinct children,
bad header/package refusal and a new verification for standalone acquisition.
It has not yet run on this checkpoint. No existing555/606 receipt is reused as
acceptance of this changed code. Source diff/check and AST evidence qualify only
the published working implementation; installed native qualification is pending
Sch's named released holder (540 is exclusively Arendt,485/334/534/style22 held).
