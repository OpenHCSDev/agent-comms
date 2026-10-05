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

On scope exit the joined cleanup retires the selected child, requires the same
local idle incarnation, admission, selected source/configuration and scope, and
restores the original source through `NativeSourcePublication`. It then acquires
the original source using mandatory shared preparation/Stats/AgentInfo, so the
restored retained child and published observation refer to the original source.
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
resource, write the original condition observation, enter the existing
`observe_native_requests`/RAM environment scope, and call original preparation
with `selected.thread` before the actual prompt. Finish capture and close that
inspector/environment scope **before** leaving the resource; restoration then
uses the original environment. This needs no production observer hook, nested
Comms root, replacement participant or second context reader.

## Source evidence and remaining boundary

Original NRA `audit.findings.Package.load` parsed 324 production, 372 test and
54 tool modules, with zero omissions. The attached census records declarations,
bases, imports, calls and source producers. Attribute matches do not resolve
dynamic dispatch by themselves; the cited owning methods were read directly.
Patterns: IDEN-1 / IDEN-2 / IDEN-3 (owner identity, declared relations, original
resource lifetime). No scanner, state mirror or alternate reader is introduced.

Production checkpoint `9636b7d9` compiles all 324 production modules. The three
authored controls in `tests/test_same_owner_native_fork.py` use the unchanged
`NativeBackendFixture` SDK producer, registered ACP owner and real custody:
one localhost prompt/completion, cancellation without input, and canonical
owner revocation/refused restoration. No fake history, witnesses, stream or
registry replacement supplies their evidence. The fork output directory is
explicitly the original owned fixture sessions directory.

These controls and Arendt's caller migration remain unqualified. All prior
675 holder and artifact purposes are closed. No runtime or package purpose is
inferred by this source checkpoint.
