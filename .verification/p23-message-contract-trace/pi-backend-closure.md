# Pi backend closure: pinned pre-cut evidence, not a completed repair

Core input is saved commit `df33d6461272fdf5b88dbc02b80612d0503dc753`, not the rebasing working tree. The author reads all 802 Python source inputs under that commit's `src/` through NRA's source snapshot. The independently owned vocabulary cut has not qualified: comms-teammate reported its original native refusal in `4b98e92ff47a`. No result here claims an after-vocabulary-cut graph or current-main packaged-path replay.

## Existing native proof and composable safe slice

- Author: `pi_backend_promotions.py`.
- All guarded candidates: `pi-backend-precut.promotions.plan.json`.
- Native reports, including every refusal: `pi-backend-precut.native-preflights.json`.
- Eligible slice: `pi-backend-precut.safe.plan.json`.
- Its native execution report: `pi-backend-precut.safe.simulation.json`.
- Command output and exit: `pi-backend-precut-safe.log` / `.exit`.

The eligible slice uses **promote_guarded_import**, not patch_target/ensure_import. It promotes AgentEvent, NativeSessionIdentity (already also bound at runtime), and LaunchRuntime. The remaining eight imported modules are refused by the original repository-cycle check: agent_events, comms, config_options, execution.native_child, execution.owned_turn, execution.turn_runner, pi_payloads and registry.threads. Script exit 0 means evidence was collected and the eligible slice simulated; it does not turn those refusals into successes. The native execution report is the verdict owner.

## Original facts and constraints the remaining cut must preserve

`PiNativeBackend.__init__` consumes the original configuration, Comms context and observer. The worker factory reads `runner.inputs.state(session_id).owned().configuration`, then `runner.comms`, and binds the existing observer. InputDrain.state, DrainedSession.owned and OwnerSession.configuration are the determining providers; a convenience factory would merely forward those same facts and can change the original refusal boundary. No such factory is proposed.

ConfigOptions supplies agent_args, launch.binary and native_environment. The last method calls ProjectRuntimeRequest.native_environment with the captured registry snapshot; that procedure requires the original process, owner identity and owner-process check before encoding the thread's launch. A new read contract must preserve that procedure, not replace it with a precomputed environment or early namespace answer. RetainedOwnerLaunch itself imports RegistrySnapshot, so importing that concrete record into a supposedly low API is not a completed dependency cut.

The backend's Thread reads include model/thinking inputs, worktree, original incarnation, runtime/writable_runtime and native_session_directory. Its environment projection additionally requires Thread.require_process and Thread.launch through ProjectRuntimeRequest. A two-field model/worktree surrogate would therefore be incomplete.

Comms contributes root, registry.snapshot, owners.require_private_launch and threads.native_launch_arguments. `prepare` acquires the environment first, then checks the private launch, then builds the native arguments. `run_turn` checks the private launch before building arguments. Those orderings are required; storing package/environment answers on a second object is not a substitute for the original providers.

TurnDelivery is the original frozen dataclass at execution/owned_turn.py:180. Its inbox, send/input boundaries, cursor delivery, progress observer, UI callback and tool mode must stay the original values/types. If lowered, move that original declaration and its concrete dependencies; do not manufacture another delivery record. InputStartListener remains a concrete imported dependency, and the UI callback still names the original DialogUiRequest/ExtensionUiChoice types.

PersistentPiSession owns the original child states and retirement order. The local imports are inside __init__, open and retain; prepare imports NativeSessionPreparation; run_turn and abort import backend. The inspected PromoteGuardedImportOperation proves TYPE_CHECKING-import promotions only. It provides no proof for hoisting these local imports. They stay explicit OPEN obligations, not silent generic-patch promotions.

## Backend replay barrier

registry/threads.py's footer imports both backend implementations and calls AgentBackend.seal(). AgentBackend.seal first requires exactly one NewThreadBackend and then freezes the registry. PiNativeBackend must be declared before that seal. Eagerly importing concrete Thread/Comms/configuration assembly while PiNativeBackend is still being declared can re-enter the footer with its class absent. Module qualification does not defer that module execution. Moving the seal or adding a post-hoc registration/factory is outside this contribution and is not proposed.

## Integration boundary

All work is artifact-only. No product edits, apply, engine edit, provider change, cold check or consumer run occurred here. The shared RuntimeEventVocabulary/PiNativeEventVocabulary and stats/notification cut remain with comms-teammate. The next combined analysis must compose the qualified shared cut on the rebased/package-correct snapshot and rerun the existing native cycle proof for the eight refused modules. Any remaining move is judged against that whole component, not one fresh-root failure per edit. This contribution supplies a usable three-import slice and the exact remaining obligations; it does not claim the requested complete Pi backend closure.
