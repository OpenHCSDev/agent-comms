# Native5 to Native6 compaction source carry

Owner: Mendel, outside `src` only. Parent owns release and live cutover;
Einstein #506 owns input provenance and compact source declarations; Arendt
#489 owns native lifecycle, journal readers and schema. Base: `95d7c03a`.

## Required relation

The existing stopped-custody carry must preserve the exact
`SelectedSummaryAttempt.source_json` bytes referenced by a native intent's
`selectedSummarySourceDigest`. Journal reset, rehashing an original intent,
rewriting native proofs or treating an archive as live enrollment/coverage
proof cannot satisfy this relation. UNKNOWN remains UNKNOWN.

Trace the current producer, original source declarations, every journal/source
reader, native intent linkage, enrollment and input coverage before extending
`tools/cutover`. No production compatibility codec, mirror or second store.
The current operator covers coordination and prompt bindings only; it preserves
the compaction journal without converting its source contract.

## Current boundary

Einstein confirms #506 is replacing full stored-input copies with ordered
original `InputProvenance` plus mandatory `TextDigest`. Its final encoding is
not yet published. The former scalar ingress/digest and nullable pending-input
source are not equivalent to the new reference contract merely by key names.

Implementation follows the final declaration and authenticated old source,
never a guessed migration. If an unchanged historical source proof cannot also
be decoded by the single current runtime format, document that conflict and
request the determining owner's correction before writing the operator.

Actual copied-original stopped acceptance comes last. No provider calls, input
replay, live owner signals, journal reset or public installation is authorized
by this draft. Preserve original journals, native sessions/proofs and donor
prefixes. Pattern leads: IDEN-1, IDEN-5, BOUND-2, TIME-9.
