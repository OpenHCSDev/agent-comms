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
original `InputProvenance` plus mandatory `TextDigest`. The compact-reference encoding was published at `de7cd35f`; the determining
owner is closing the historical-attempt reader contract. The former scalar ingress/digest and nullable pending-input
source are not equivalent to the new reference contract merely by key names.

Implementation follows the final declaration and authenticated old source,
never a guessed migration. If an unchanged historical source proof cannot also
be decoded by the single current runtime format, document that conflict and
request the determining owner's correction before writing the operator.

Actual copied-original stopped acceptance comes last. No provider calls, input
replay, live owner signals, journal reset or public installation is authorized
by this draft. Preserve original journals, native sessions/proofs and donor
prefixes. Pattern leads: IDEN-1, IDEN-5, BOUND-2, TIME-9.


## Actual running-source inventory

Source observed: `/var/tmp/agent-comms-live-20260927-wzjtqhza/compaction-commits.sqlite3`.
Classification: **running-source-inventory, not stopped carry**. A mode=ro,
query-only SQLite transaction supplied schema, counts and a private logical
backup. The source file revision was unchanged; observed SHA256 was
`296e6da164bd7e16fc086e83a9f509dccdf12c4e33876c1f854b26a78d9132b1`,
446464 bytes. The inventory took 0.038554 seconds; no public write, native
input, provider call, owner stop or replay occurred.

| Canonical table | Observed rows |
| --- | ---: |
| selected_summary_attempts | 4 |
| operations | 3 |
| publications | 3 |
| private_raw_inputs (UNKNOWN) | 341 |
| enrolled_private_sessions | 0 |

All four sources use the original scalar `ingress_key`/`original_digest`
encoding. Three attempts are linked to committed native operations; all three
`selectedSummarySourceDigest` values equal SHA256 of their exact original
`source_json`. One attempt is refused and has no native commit reference.
These facts do not grant new original-input admission or change disposition.

Each attempt's frozen retained facts contains exactly one original input with
the same ingress key, explicit human origin, and text digest equal to the
recorded original digest. This supplies a concrete provenance reconstruction
lead from frozen evidence rather than today's registry. The shape/equality
inventory is not declaration authentication: the outside-src carry must decode
with the authentic original declarations and certify the whole target request.

Sanitized observations are committed under
`evidence/native-compaction-source-carry-20261002/running-source-inventory01/`.
Only hashes, shapes, counts, resource revisions and paths are exposed. The raw
logical SQLite backup remains private (0600, parent directory 0700) under
`/home/ts/.cache/agent-scratch/comms-native-compaction-source-carry-20261002/running-source-inventory01/`.
Its hash is `4d48c65ab94a77eeecc8c5aaf026b7184d632252736a3897025bbc3e6792dee4`;
it is not a byte-identical copy or a stopped donor. No raw database is committed.

## Determining owner and remaining carry relation

The strict current decoder cannot decode historical scalar source bytes, while
rehashing those bytes would invalidate the original native commit reference.
Einstein #506 and Arendt #489 own separating the immutable original native
proof input from the one current typed semantic source on the existing attempt
and closing every source/envelope/outcome/admission/recovery consumer. Mendel
#510 extends the existing stopped carry only after that declaration/DDL is
published. Original source bytes, native payload/metadata/reference digests,
operation and publication states, all UNKNOWN rows, enrollment and coverage
must survive unchanged. No runtime old reader, mirrored request authority,
reset or archive-as-admission is acceptable. Patterns: IDEN-1, IDEN-5, BOUND-2,
TIME-9. No carry acceptance or installed-readiness claim is made here.
