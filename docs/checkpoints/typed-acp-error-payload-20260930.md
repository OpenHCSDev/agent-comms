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
introduced. Installed SDK/ACP/native failure/history acceptance is pending.
