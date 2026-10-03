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
