# Typed external ACP failure payload ownership

Owner: Arendt. Scope is the external payload boundary in `acp_failure.py` and all
its callers; it is independent of frozen456/458/459/462 and the parent-only quiet
cutover. Base current main1f42575c. No public mutation or input replay.

## Required relation and deletion trajectory

`StructuredErrorValue`, `ErrorReasonField` and `ExternalFailureData` currently
use MroDispatch handlers keyed by dict/list/str, with raw pending/seen/detail
mutation and `handlers_for` introspection. This is IMPL-3 type dispatch and
BOUND-1 raw structure handling hidden behind nominal dispatch names.

Decode the provider's external nested JSON once through the original FieldCodec.
Nominal payload shapes own their detail rendering and external reason precedence.
Canonical PromptFailureReceipt, InputAttempt and PiDiagnostic continue to own
notification/disposition/diagnostic facts. Plain text, arrays, nested error/data
and JSON encoded in external reason text remain supported external formats.
Unknown/redacted disposition is not retry authority.

Delete the three old traversal/dispatcher definitions and every consumer. No
builtin keyed handlers, FieldCodec subclasses, alternative codec, semantic cache,
compatibility reader, mirror or new delivery status state. Extend shared field
capabilities only with the parent FieldCodec owner if the boundary lacks one.

## Acceptance

Focused provider failure regressions plus the existing real installed SDK/ACP
failure journey with a controlled localhost provider. Preserve original failure
receipt publication, plain and structured provider detail, array ordering,
NotSent versus UNKNOWN and diagnostics. No paid calls or original UNKNOWN replay.
Tests alone do not establish installed readiness. Publish source checkpoint and
production deletion counts, then the actual affected installed receipt.

## Owner item 5 review

Review touched source and the assigned456 family for newly introduced None as
semantic state. Existing nominal model/input/proof owners remain authoritative;
legitimate optional external fields are classified separately. Do not edit the
frozen456 production candidate or create local status mirrors.

## Source checkpoint

Normally merged the sole FieldCodec contribution88c9e03f. ErrorValue derives
from DeclaredFamily and JsonShapeFamily in that order. Seven concrete external
JSON shapes each declare one typed value field and opt into JsonShapeMember;
semantic receipt/disposition/diagnostic members do not enter shape selection.
The original codec alone classifies JSON primitive shapes. No codec subclass or
raw builtin keyed handler remains in the error boundary.

Deleted113 production lines including all three traversal/dispatch owners and
their consumers. Added206 production lines for typed shape rendering and
canonical metadata ownership. BOUND-1/2/3 and IMPL-3/4: decode external children
once; reason precedence belongs to the external object; encoded provider text
cannot grant original-root disposition. Original receipt detail and diagnostic
text bypass provider reason interpretation. Canonical metadata replaces its
generic decoded subtree; it is not retained alongside the original owner.

Focused59 controls pass in0.31s: failure rendering, typed transport diagnostics,
existing FieldCodec contracts and the shared JSON shape controls. The existing
new display declaration experiment needs one member declaration and no factory
case. Explicit JSON null is an external shape; no nullable semantic field was
introduced.

## Installed acceptance

Source500bf7dd was built as a normal wheel and installed with --no-deps into
this worktree's .observations/error-installed. All291 Python modules are byte
equal to source. SDK0.12.1 and the existing593b native package came from the
normal installed cohort; no editable import, source fallback or global package
change. Actual installed journey exit0, sanitized receipt:
`evidence/typed-acp-errors/installed-sdk-acp-native.json`.

The real SDK/stdio ACP child attaches to a private native owner, creates one
saved reply, closes, opens saved history in a fresh ACP child, and sends one new
synthetic input. The localhost provider returns400 with a JSON-encoded nested
object/array reason. SDK RequestError and exactly one request_failed notification
carry the same canonical ProviderQuotaFailure/StartedInput. A third fresh child
passively attaches to failed history: provider posts stay2; native bytes and
InputDocument stay unchanged; two original native users remain. The private
owner is explicitly stopped and process_alive is false before exit0. Public
effects0, paid calls0, original/replayed inputs0.

Exact command, from this worktree (a new evidence path is required per attempt):

```sh
PYTHONPATH=/home/ts/wt/comms-typed-acp-error-payload-20260930/.observations/error-installed \
 /home/ts/.local/share/agent-comms/runtime-canonical-bus-input-visibility-20260930/bin/python \
 tests/acp_error_payload_installed_journey.py \
 --installed /home/ts/wt/comms-typed-acp-error-payload-20260930/.observations/error-installed \
 --package /home/ts/.local/share/agent-comms/native-current-593b978a717ae8f6/node_modules/@earendil-works/pi-coding-agent \
 --evidence /home/ts/wt/e463-run03
```

Two earlier private attempts loaded the same session twice in one ACP child and
received two equal failure notifications. Source inspection identifies
SessionLifecycle.attach_owner replacing proxies[session_id] without closing
the original proxy. Sch received this disjoint attachment resource defect and
the sanitized `repeated-load-duplicate-failure.json` witness. The error-family
checkpoint does not claim that separate resource defect fixed. All three roots
e463-run01/02/03 retain their native journals/dispositions/diagnostics; all
owners stopped. Owned wheel/target disposable artifacts are under .observations.

No persisted wire/store/phase ABI changes in this error-family checkpoint;
no owner restart or runtime reset is required for its contract. Parent owns
merge and subsequent matched installation. CI deferred; no global activation
or public input occurred during acceptance.
