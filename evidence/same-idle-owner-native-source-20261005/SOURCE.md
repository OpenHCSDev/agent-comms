# Same-owner saved fork selection

Source checkpoint from merged main `7b68d630caa654d3e9884f45cbd6493718752a0c`.
This is an implementation draft; no installed or provider acceptance is claimed.

## Existing facts and owners

- `NativeForkCreation` owns the SDK-returned child identity, original parent,
  creation revision, prefix digest and recorded ancestry. Its journal row and
  acquired original bytes must corroborate selection. It grants no USER task,
  input disposition or current registry admission.
- `RegistryOwner.capture_local` already captures an idle local executor. The
  registered incarnation, process, admission, source/configuration and current
  goal remain registry-owned. An idle source change must preserve these facts.
- `Registration` / `RegistryDocument` / `NativeSourcePublication` own a durable
  source publication and its original publication-identity exclusion. The
  admitted `attach_native_session` path remains turn-fenced.
- `SessionLifecycle` owns the loaded ACP binding. `TurnRunner` owns the session
  turn mutex. `PersistentPiSession` and `NativeCustody` own retained child
  retirement, acquisition and attestation.
- `TaskScope` and the certified wire task declarations own current USER scope.
  Native parent/child relation is only history evidence, never task authority.

## Concrete missing relationship

`ThreadManagement.attach_session` performs a whole-thread comparison before
initial attachment, but has no SDK-fork corroboration or retained child lifetime.
`SessionLifecycle.bind_owned` retires a foreign proxy, not the retained native
child. Combining either with a caller-authored registry replacement is not a
same-idle-owner source-selection resource.

The existing native reopen state means reloading the **same** saved identity
after an external write. It cannot represent switching to a different SDK fork.
The selected fork needs retirement of the previous child and acquisition through
the existing mandatory preparation/AgentInfo observation path.

## Implemented closure

`SessionLifecycle.selected_native_fork(session_id, original, creation)` composes
the existing loaded binding, `TurnRunner.idle_backend` and the registry resource.
`NativeForkCreation.require_recorded_selection` corroborates the stored SDK row,
unchanged original parent cut and child prefix. `RegistryOwner` requires the
local idle owner and current task scope. Publication changes only the saved
source. No attachment, turn, persistent, wire or reader lock survives the yield
to the existing constructor, observer and prompt.

On entry, the joined registry observation first validates the original idle
owner and captures its existing `LiveThreadOwnerBinding`. Before starting a stop,
`PiSessionChild` checks the actual launch saved identity, original encoded
`ProjectRuntimeRequest.binding`, and the existing `NativeArguments` model/thinking
projections plus launch worktree. `NativeCustody` owns refusal and delegates
retirement to its original stop algorithm. Empty custody does no work; retiring
custody joins its existing task; a successor child is refused without being
stopped or replacing its custody.

Selection enlists the exact installed owner in the original `AsyncExitStack`
BEFORE registry commit. A cancelled worker result therefore cannot discard the
cleanup owner. `Registration.restore_native_fork` is an explicit guarded
publication, replacing the deferred generator exit. A failed native retirement
cannot trigger a later source restoration through generator destruction.

On scope exit, qualified retirement can join the original selected child even
after registry revocation. The registry independently requires the same local
idle incarnation, admission, selected source/configuration and scope before
restoring the original source through `NativeSourcePublication`. It then acquires
the original source using mandatory shared preparation/Stats/AgentInfo while
the original turn AND persistent child locks remain held. The restored retained
child and published observation refer to the original source.

`NativeSessionPreparation.open` still owns ordinary lock acquisition. Its
`open_acquired` capability owns the same launch/attestation/writer fence/Stats/info/
retention/terminal cleanup body when restoration already holds that lock. The
existing `TurnRunner.prepare_selected_session` consumes this opener; normal
callers keep the locking opener. The old release-then-prepare gap and nested
lock reacquisition are both deleted.
Keep legitimate completed-turn fields and original input receipts. Loss of the
owner/source/scope must refuse restoration, not overwrite a newer owner. UNKNOWN
remains UNKNOWN; no input is manufactured, replayed or settled by selection.

All source-change consumers in `BEFORE.json` are read as initial attachment,
admitted source observation, stopped restoration, or actual source selection;
these lifetimes must not be merged merely because they write `session_file`.
The new original consumer is Arendt #674's condition application. Arendt owns
the private task publication/reader and caller migration; Mendel owns the source
selection/retirement methods. No current overlapping production writer.

The existing conditional configuration retirement now uses the same turn/child
resource. A held turn mutex refuses an idle borrow, including calls from that
turn, instead of waiting on its own lock. The old check followed by unrelated
child acquisition is deleted. Initial text and image acceptance also share one
`InitialInput.run` call; the repeated text-only invocation is deleted without
changing relay behavior or input ownership. The two registry source paths use
one document-owned publication construction.

Original initial attachment and leased source observation remain distinct from
this certified fork resource. No fork row grants another admission, recipient,
USER author or task declaration. No input, journal or task record is altered by
selection/restoration.

## Consumer contract for #674

Capture the original idle `RegistryOwner` from the same service snapshot and
pass the returned SDK creation recorded in that root's journal. Inside the
resource, receive `(selected, retire_selected)`, write the original condition
observation, enter the existing `observe_native_requests`/RAM environment scope,
and call original preparation with `selected.thread` before the actual prompt.
In that inspector/environment scope's `finally`, `await retire_selected()` joins
ONLY the qualified original selected child while the inspector is still alive.
Then close the inspector/environment scope **before** leaving the selection
resource; restoration uses the original environment. No caller-authored current
child close or registry replacement supplies retirement authority. This needs no production observer hook, nested
Comms root, replacement participant or second context reader.

## Source evidence and remaining boundary

Original NRA `audit.findings.Package.load` parsed 324 production, 372 test and
54 tool modules, with zero omissions. The attached census records declarations,
bases, imports, calls and source producers. Attribute matches do not resolve
dynamic dispatch by themselves; the cited owning methods were read directly.
Patterns: IDEN-1 / IDEN-2 / IDEN-3 (owner identity, declared relations, original
resource lifetime). No scanner, state mirror or alternate reader is introduced.

## Corrected counterexamples and source checkpoint

Parent read `9636b7d9` and found that generic `persistent.close()` ran before
canonical stale-owner refusal. A newer same-name owner could acquire a child
that the stale selection or restoration would then stop. That source checkpoint
is superseded; its build-only purpose was withdrawn before use.

The corrected production checkpoint is
`a264da8f7251c72fae0ee0259c1d81de9ea5ed7e`. Original Package parsed 324 production,
373 test and 54 tool modules with zero omissions; `AFTER.json` records 16 existing
owner classes, 37 owning methods and 45 direct or indirect attribute consumers.
All 324 production modules and the authored control source compile. This is
source evidence only; no application import, SDK/native execution, test, build,
package installation or provider operation qualified this checkpoint.

The seven authored variants in `tests/test_same_owner_native_fork.py` use the
unchanged `NativeBackendFixture` SDK producer, registered ACP owner and real
custody: one localhost prompt/completion, cancellation without input, canonical
owner revocation/refused restoration, and entry/exit successor refusal for both
owner generation and registered configuration changes. Owner successors are
produced by original idle fencing, heartbeat and same-process acquisition;
configuration successors use `ThreadManagement.set_thread_thinking_level`.
Actual preparation replaces the prior child before the refusal is exercised.
The cases require the successor child, registry source, owner/admission and
configuration to remain intact until ordinary fixture shutdown. No raw registry
replacement, fake history, witness or process supplies these facts.

During original parent AgentInfo publication, the completion/cancellation cases
observe BOTH original locks held and schedule an actual native context read that
must remain pending until restoration releases custody. That read is then joined
and must return the restored source; it sends no prompt. This covers the real
release-then-prepare source race without a second reader implementation.

The controls and Arendt's tuple/finally caller migration remain unqualified.
No current package build/import/READ/EXEC purpose is inherited from the withdrawn
`afac6739` request or completed #675. A fresh purpose must bind this corrected
source, unchanged shared fixture, seven authored variants and the actual final
installed proof before execution. Original UNKNOWN and prior negative receipts
remain unchanged. No latency or provider-performance claim is made.
