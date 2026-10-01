# S2 original input consumer closure

Status: draft source checkpoint. Final bounded validation remains outstanding.
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

Reasoning and coherent source closure precede validation. Final validation will
reuse the existing continuous pin/query/export/correction/drop journey and
exercise the installed CLI in an owned private fixture without provider/native
inputs. This is not a claim of full native/UI acceptance. No tests have been run
for this checkpoint. No provider calls, native builds, public writes or restarts
have occurred.
