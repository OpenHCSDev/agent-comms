# S2 original input consumer closure

Status: coherent source and installed CLI closure validated; draft for owner review.
Canonical 499 qualification is independent; no public activation is requested.

## PR base and exact ancestry

PR500 is stacked on PR499's `fix/selected-native-compaction-budget-20261001`
branch. It must not merge independently while 499's real fresh501 fork
qualification is running. Retargeting does not rewrite the branch or alter source.

Included ancestry, in order:

- Original481 producer head `34e3dd875e9282b8a71a9b16ca80fb0157f21b16`.
- Main `9954cdd73dff20e590aebf15467a170f0a69e799`, normally merged at `9b1238ee`.
- Core499/native5184 checkpoint `07934d583cd1175f17a8843d3e7feec5093c2fb5`,
  normally merged at `cfbeb06a`.
- Consumer source closure `58dfb83b` and installed CLI validation `96f517e0`.

PR499's head at retarget review was `496bd8a4029deb4b6d60795eae1b13a696d3e914`;
those newer499 changes are not claimed as merged or validated here. The stacked
diff still intentionally includes the481 original producer extensions in
input_attempt.py, input_origin.py, messaging.py, retained_task_facts.py,
task_sources.py and turn_context.py. Only the four declared consumer seams are
new consumer implementation. The inherited499 native5184 and compaction changes
are qualified by499's owner, not independently by500's CLI fixture.

After qualified499 merges, normally merge main into500 and restore its PR base
to main. Review `58dfb83b` and `96f517e0` as the consumer commits, retaining the
explicit original481 producer review. Existing installed CLI evidence remains
valid at its recorded strength; no provider qualification repeats are requested.

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
