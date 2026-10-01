# S2 original input consumer closure

Status: coherent source and installed CLI closure validated; draft for owner review.
Canonical 499 qualification is independent; no public activation is requested.

## Existing owners and caller closure

Source inspection located the original 481 producer in InputOrigin,
StoredInput, InputProvenance, Messaging.pin_input_constraint,
NativeInputConstraintPin, and RetainedTaskFacts.original_text_source.
MessageReference owns wire references only and is unchanged. LockedStore.reading
owns the original input document lifetime. WireLog.retained_context is the
existing 474 query owner. RetainedFormat and PinConstraintCliCommand are the
existing export and CLI families.

The producer already authenticates original human provenance, recipient, author,
project, and root. Its consumers previously assumed original wording was always
a Message: the query omitted StoredInput facts, the formatter read Message-only
fields, and the CLI exposed only wire references. This change extends the
existing TaskAttachment family for original retrieval, wording and provenance;
NativeInputConstraintPin supplies its original StoredInput semantics. The query
joins authenticated input facts into the same frozen RetainedTaskFacts before
owner projection. The formatter derives content and provenance from that fact
set. PinInputConstraintCliCommand inherits the existing pin publication workflow
and adds the genuinely distinct durable-input-key boundary.

The granted consumer claims are exactly retained_context.py, cli_commands.py,
task_sources.py, and WireLog.retained_context in wire_log.py. The original 481
producer is included in this branch; main 494 framing and the merged 499
CoordinationSegment/native manifest remain intact. Sch owns the original query
and compaction; Arendt was informed of wire -> bus -> registry snapshot -> input
read acquisition. No input state, UNKNOWN disposition, native custody or replay
meaning is changed. Original sources remain in their existing stores.

## Delivery boundary

Reasoning and coherent source closure preceded validation. Final focused
validation passed three checks in 2.21 seconds using the installed wheel. The
installed CLI pilot passed 15 subprocess calls: three original-input pins,
query/export, four expected refusals, rename, correction, drop and reopen.
The two distinct originals had identical wording; repeated pinning shared only
the same original provenance. Original input file bytes remained unchanged after
every command and both originals remained unresolved. Eight installed producer
and consumer module hashes match the reviewed checkout. No source overlay or
conftest scheduler replacement was used by the installed pilot. This verifies
the affected installed CLI path; full native/ACP/UI acceptance is not claimed.
No provider calls, native inputs, native builds, public writes or restarts occurred.

## Persistent evidence

`evidence/original-input-consumer-closure-20261001/installed-journey.json` records
the installed entrypoint, module hashes, all command outcomes and original-byte
preservation. `existing-owner-caller-search.txt` includes the actual declaration
and consumer searches. It shows a single TaskAttachment declaration, one
NativeInputConstraintPin specialization, and one inherited PinInputConstraintCliCommand
member; the two retrieval/wording implementations are ancestor and native override,
not competing authorities. `merge-main-preimages.json` records unchanged494 framing
and original481 source lookup. The fixture and small installed wheel environment
are owned under `.artifacts/direct-input-consumer`; no native environment was built.
