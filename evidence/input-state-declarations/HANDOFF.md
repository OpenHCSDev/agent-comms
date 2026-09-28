# Input state declaration batch

## Parent API

`InputDocument.lookup(key: str | None) -> InputAttempt` always returns the existing family. Leaves: ReservedInput, BoundUnknownInput, StartedInput, NotSentInput, MissingInput.

All states have boolean `exists`, `accepts_reservation`, `has_started`; methods `matches_owner(ThreadIncarnation)`, `matches_admission(int)`, `matches_native(...)`, `bind(...)`, `started(...)`, `finish_unbound()`, `pending_for(Thread)` and `queued_for(ThreadIncarnation, admission, text)`.

Stored states own `digest: TextDigest` from source_text. Sent states own nonoptional turn_id/native_id/sent_text and sent_digest. MissingInput is fieldless, so guard `exists` before reading stored fields. ReservedInput has no sent fields. No unattempted/UnknownInput compatibility API remains.

Stored constructor remains `(key, sequence, owner_name, admission, target, source_text)`, with keyword-only notice/review fields. Historical rows never recorded a creation time: matches_owner checks the recorded name; parent's selected-source-to-live-owner fence must match full ProcessIdentity/ThreadIncarnation. Never infer incarnation birth from today's registry.

No new FieldCodec subclass. Existing input store decodes the single established flat format once through state declarations and plain FieldCodec. Native-binding field roster is derived from SentInput dataclass declarations. Public unknown status remains an external projection of reserved/bound states.

## Scope/dependencies

Parent owns text_digest.py, selected_source.py, reservation rules, compaction callers, owned_turn.py, input_drain.py. This batch references the agreed TextDigest.of/matches API and does not reimplement it. Parent must migrate their former nullable input reads before integrated owner paths can run.

## Evidence

Read-only actual stored input sample: all 7,766 rows round-tripped exactly (2,555 reserved; 5,189 started; 22 bound unknown). No stored bytes were changed, no history omitted and no birth dates invented.

60 focused tests passed in 4.15s: durable state lifecycle, rejected malformed/partial binding preservation, saved notices/reviews, native routing repair and continued-session evidence. Includes actual owner socket notice updates with the existing real private-bus test issuer; zero provider requests.
