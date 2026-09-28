# Input state declarations — current contract

## Final schema (supersedes checkpoint 3912eb18's flat-format preservation)

The owner's newest instruction requires current-only inputs. Plain FieldCodec stores the declaration-derived `kind` and nested dataclasses. There is no InputAttemptCodec, alternate reader, flat-format normalizer, or null-field writer. Prior nullable records are rejected without mutation. Parent owns cutover of historical input data and runtime journals; nothing live was modified here.

Root is **InputAttempt**, not InputRow; no alias. Leaves: ReservedInput, BoundUnknownInput, StartedInput, NotSentInput, MissingInput.

`InputDocument.lookup(key: str | None) -> InputAttempt` always returns a state. MissingInput has no fields. All states have boolean exists, accepts_reservation, has_started and methods matches_owner(ThreadIncarnation), matches_admission(int), proves_started(...), matches_native(...), bind(...), started(...), finish_unbound(), pending_for(Thread), queued_for(ThreadIncarnation, admission, text).

Stored states own digest: TextDigest from source_text. Sent states own nonoptional turn_id/native_id/sent_text and sent_digest. Reserved and NotSent declarations and persisted records have no sent fields. MissingInput must be checked via exists before reading stored fields.

`proves_started(*, owner:ThreadIncarnation, admission:int, turn:TurnId, sent_digest:TextDigest, original_digest:TextDigest) -> bool` is false on the base. StartedInput owns direct-input sequence/target, recorded owner/admission, exact turn and both digest comparisons. This is the parent journal's proof API.

Stored constructor remains `(key, sequence, owner_name, admission, target, source_text)`, with keyword-only notice/review fields. Row provenance remains recorded name/admission; the parent source-current-owner rule compares the full incarnation and ProcessIdentity. New source uses separate `owner: ProcessIdentity` and `incarnation: ThreadIncarnation`, so call row.matches_owner(source.incarnation).

## Ownership

Parent owns TextDigest, TurnId, selected sources, reservation rules, compaction/owned_turn/input_drain callers and live cutover. The sidecar does not add replacements for these shared domains. Parent owns continued_private_session's source migration. The input-row-only change already in 3912eb18 uses has_started and deletes redundant nullable sent-field checks there.

## Tests

Current-only input/non-compaction batch: 78 passed, 8 proof cases deselected in 35.82s. Proof cases: 8 passed in 0.06s against an isolated copy of this source plus the parent's actual uncommitted text_digest.py and thread_identity.py (no mocks/provider calls); publish/merge those parent files before combined installed acceptance.

The earlier read-only inventory proved all 7,766 historical rows were understood (2,555 reserved, 5,189 started, 22 bound unknown). It is inventory for the parent's explicit cutover, not evidence of a retained old-format reader. The current source rejects that old format by design.
