# Selected reservation ownership cleanup

Latest owner correction applied: no SelectedSourceCodec, no incarnation source
field encoder/decoder, no birth_key. Source records are a DeclaredFamily with
its derived kind. FieldCodec encodes nested ProcessIdentity, ThreadIncarnation,
TurnId and TextDigest directly. Only admission sources have ingress_key.

Reservation, commit and interrupted recovery now share named ReservationRule
classes. Row checks are shared with final original-input admission. The raised
rule identifies the refusal. Rules are discovered from declarations, including
the tested new-case extension. Manual and admission recovery dispatch through
their source declarations. NotSent proves no binding for retirement, while
remaining ineligible to bind or replay the original.

ReservedInput has no native_id, turn_id or sent_text fields. BoundUnknownInput
and StartedInput own sent fields; MissingInput represents failed lookup.
TextDigest owns original/sent UTF-8 digest comparisons. Historical input names
are compared with their recorded admission generation; they do not pretend to
contain an unrecorded birth timestamp. Commit additionally compares the current
ProcessIdentity, and recovery compares the full ThreadIncarnation.

The permanent AST guard rejects all FieldCodec subclasses outside field_codec.py,
including aliases and inherited imports. MessageWireCodec, TranscriptCodec,
TranscriptRoutingStorage and duplicate claim parsers/serializers were deleted.
Durable bus claim/message encoding is retained through declaration metadata.

## Lines relative to the rejected recovery implementation (7c51d789)

- Production: 484 deleted, 690 added.
- Tests: 405 deleted, 803 added.

Production grows because named refusal rules and disjoint input states replace
implicit field combinations. Tests grow for the requested permanent ownership
guard, rule extension/refusal coverage and real native failure integration.
The uncommitted SelectedSourceCodec was removed before publication and is not
counted as a deletion from that Git baseline.

## Evidence

- nominal-verified.log: 134 passed, 2 skipped. Includes source identity/rule
  behavior, real SQLite crash/no-replay checks, actual native manual compaction,
  native provider HTTP400/429 failure and disconnect, and codec ownership guard.
- nominal-installed-native.log: 10 passed against the noneditable candidate.
- Additional S9 and codec architecture guards: 5 passed.
- live_nominal_fork.json: installed ACP stdio -> actual selected Pi ->
  openai-codex/gpt-6-sol -> native compaction committed -> LIVE_COMPACTION_OK.
  The 143686055-byte original session was unchanged; no original input replayed.
  Final manual-recovery dispatch and named-error propagation were subsequently
  included in the rebuilt installed candidate and its ten native checks.
- Earlier failed receipts are retained honestly. Initial broad owner-integration
  fixture tests failed before their assertions on fake/fake provider startup and
  an obsolete selected response envelope; this is not a full-suite green claim.
- The completed provider-test owner is stopped and its 143 MB private session
  copy removed. Sanitized receipt and small private journal remain.

## Installation contract

NOT installed on the live route yet. During a quiet cutover with no turns or
compactions in flight, reset compaction-commits.sqlite3 (and SQLite sidecars) and
input_dispositions.json. Both are runtime stores. Relaunch owners on the same
new core/native pair. No old-format reader, converter, replay, or journal
reconstruction is added. Native Pi sessions/input proofs, durable wire messages,
goal history, owner decisions and durable routing annotations are retained.
The complete journal reset also discards transient private enrollments and
selected-operation rows; fresh observations use only the current schema.
