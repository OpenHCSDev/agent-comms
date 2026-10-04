# Existing native session/revision owners cross their boundary as values

Receiving integrated source: main `1f8bf0ef` plus Sch #494 `cc534692` normally merged
into #489. This source change extends existing owners; no new identity, revision,
codec, registry or permission family is introduced. Validation follows the complete
integrated lifecycle implementation.

## Source ownership decision

`NativeSessionIdentity` already declares the SDK session ID/file relation and
validates completeness. `NativeWitness` adds a cutpoint for that SAME session; it
now inherits the identity fields/behavior instead of redeclaring and independently
validating them. Session comparison belongs to the existing identity's `same_session`
relation, which compares the full ID/file pair even for a cutpoint subtype. Retained
child selection still checks its actual package, current source and original child.
A witness does not enroll a session or grant native admission.

`FileRevision` already owns the physical device/inode/size/mtime/ctime observation.
Main's `NativeRevisionText` is the existing original FieldCodec field capability for
the SDK's five-component scalar. NativeWitness.revision, OwnerCompactionAttestation's
native revision and NativeCommitPosition/aborted-no-write receipts now carry that
original FileRevision, using `Annotated` only at the external field boundary.
Consumers do not receive a string and reconstruct its meaning.

NativePreparationResult.checked consumes FileRevision directly. The preparation
helper still emits the same SDK scalar; FieldCodec decodes it once. Witness current
file checks compare the original FileRevision directly. Reopen validation also
consumes FileRevision, deleting its independent five-term tuple encoder.

SelectedSummarySlot now passes the original witness to the existing child identity
relation, then compares witness.revision with SessionRevision.native. Delete
SessionRevision.native_stamp and its final committed-outcome consumer; neither
consumer stringifies a revision just to compare it. SessionRevision keeps its
separate original input-proof observation and exact source checks.

## All consumers and unchanged boundaries

- PrepareCompactionHelper/NativePreparationResult/NativeWitness: decode and bind
  the exact returned cutpoint to the original stat observation.
- NativeWitness.require_current_file and NativeEvidenceRead.retained_task_facts:
  original acquired source rechecks consume that typed revision.
- Threads.compaction_attestation/Registration/CompactionBoundary: retain the same
  original owner/process/goal/turn grant; carry the typed native observation without
  altering the separate registry revision or allocation domains.
- SelectedSummarySlot/NativeCustody: original child/session/package/source checks;
  no copied NativeSessionIdentity wrapper around witness fields.
- NativeIntent/NativeRequest/native writer: FieldCodec encodes the witness's existing
  SDK scalar. Native request/payload digests therefore keep their original format.
- NativeOutcome/NativeCommitPosition/CompactionPublishedMetadata: original returned
  revision becomes FileRevision at decode; publication derives from that receipt.
- SelectedSummaryAdmission/CommittedNativeOutcome: exact original saved-source
  revision comparison consumes SessionRevision.native, not a string projection.
- CompactionPublication/ACP extension: original metadata decoded/encoded by the
  same codec. Toad's original CompactionPublishedUpdate handler requests its existing
  transcript checkpoint; it has no scalar-revision parser or admission decision.

Native SDK JSON remains flat and uses the same field names and colon revision.
The TypeScript/JavaScript SDK schema is an external boundary, not a second Python
identity declaration. No native artifact, journal schema or original input/proof
format changes in this relation. Sch's typed source/intent/outcome framing is
preserved by normal integration. UNKNOWN and original histories remain untouched.

[Authored source references](../../evidence/runtime-lifecycle-semantic-pass-20261001/phase-boundary/core-consumers.json)
include every FileRevision/NativeSessionIdentity/witness/revision-field consumer.
Each determining class is declared once; captured cutpoints, receipts and owner
attestations are distinct observations of those typed facts, not competing
comparison rules. NativeRevisionText.decode/native_stamp calls in consumers are
gone. Class counts alone are not proof of runtime correctness.

Patterns: BOUND-8 (owned revision flattened at its boundary), IDEN-1/3 (same physical
fact recoded), IMPL-12/13 (repeated decoding/tuple implementation). A new SDK receipt
uses the existing field capability and the existing FileRevision. It introduces
neither a revision parser nor another file identity class.
