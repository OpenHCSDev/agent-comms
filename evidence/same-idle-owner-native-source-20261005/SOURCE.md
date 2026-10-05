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

## Intended closure

Extend the existing loaded-session resource, registry source publisher and fork
record owner. Selection must corroborate the stored SDK creation and parent cut,
retain the original local idle owner and current task scope, retire the original
child, and publish only the child source. Yield without holding turn, persistent,
wire or source-reader locks needed by the original constructor and prompt.

On scope exit join native work, require the same local idle incarnation,
admission, selected source/configuration and original scope, retire the selected
child, and restore the original source through the same registry publisher.
Keep legitimate completed-turn fields and original input receipts. Loss of the
owner/source/scope must refuse restoration, not overwrite a newer owner. UNKNOWN
remains UNKNOWN; no input is manufactured, replayed or settled by selection.

All source-change consumers in `BEFORE.json` are read as initial attachment,
admitted source observation, stopped restoration, or actual source selection;
these lifetimes must not be merged merely because they write `session_file`.
The new original consumer is Arendt #674's condition application. Arendt owns
the private task publication/reader and caller migration; Mendel owns the source
selection/retirement methods. No current overlapping production writer.

## Source evidence and remaining boundary

Original NRA `audit.findings.Package.load` parsed 324 production, 372 test and
54 tool modules, with zero omissions. The attached census records declarations,
bases, imports, calls and source producers. Attribute matches do not resolve
dynamic dispatch by themselves; the cited owning methods were read directly.
Patterns: IDEN-1 / IDEN-2 / IDEN-3 (owner identity, declared relations, original
resource lifetime). No scanner, state mirror or alternate reader is introduced.

Implementation and the actual same-owner saved-SDK selection / one prompt /
restoration / owner-loss and cancellation acceptance are unfinished. All prior
675 holder and artifact purposes are closed. No runtime or package purpose is
inferred by this source checkpoint.
