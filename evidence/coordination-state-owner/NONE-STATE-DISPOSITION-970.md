# Item 5 — #457 final integrated None disposition

Owner: Mendel. Reviewed integration **970bc527f4ddd9b3bde5522dc671aea11fd27ece**
against original base **6feb634ba184d94d763028406152fff07027cccc**.
Accepted #457 source/evidence remains **e1f2d732bb9c8f447da12b5fbc0b31519860cdc0**.
This receipt updates the already published #462 `NONE-REVIEW.md`; it performs
no production changes, tests, provider calls or SessionRevision implementation.

## Source attribution and deletions

The four own production commits are `51631c9b`, `82713f9b`, `84a76b18`,
`7f64d7ac`: **449 deleted / 749 added** cumulatively. This counts repeated
edits, not unique removed lines. The final integration versus original base on
the 21 owned files is **459 deleted / 756 added**, including peer changes.
`NONE-STATE-DISPOSITION-970.json` lists exact commits, every file, blob IDs,
counts and final locations for all **57 own added None text sites** from the
existing census. Two retry lines were rewritten in the Todo checkpoint;
final `coordination_tables/executions.py:123` still denies missing replay and
requires the original retryable wire obligation.

## Declaration and complete consumer disposition

| Final owned sites | Specific meaning of absence; lifecycle owner and callers |
| --- | --- |
| `assignment_states.py:161,191,309`; `assignment_store.py`; `coordination_tables/assignments.py` | Execution/target absence is the **existing unbound resource** of declared preengagement members. `AssignmentState` alone admits successors. Triage forbids and engaged framing requires the original obligation. Store and wake framing invoke these declarations. No absent-value lifecycle selector was added. |
| `execution_states.py:57,205`; `coordination_snapshot.py`; `coordination_tables/executions.py:123`; `attempt_states.py`; `coordination_tables/attempts.py` | Attempt/ordinal/replay/obligation are **existing acquired resources or related SQL lookup results**. Missing replay denies authority; missing acquired attempt cannot prove terminality or retry. Execution and terminal-attempt declarations own the decision used by snapshot and recovery. This is not a blanket SQL exemption. |
| `envelope_claim_transitions.py:259,283` | An existing manual claim may lack native admission. Selected native claims require the actual typed WakeAdmission and original source. ClaimOwner/ClaimProjection own different resource/frontier checks; no new optional lifecycle field. |
| `input_attempt.py:278`; `thread_identity.py` | A mismatched exact native witness yields **no transition result**; the durable UNKNOWN row remains unchanged. InputDispositions applies only a returned declared input. Unit-return validators carry no state. Native-ID parsing now uses the existing declaration boundary. |
| `todos.py:83,256,290,319` | Assignment remains the **original optional owned resource**; TodoState owns lifecycle. Newly declared command generation is an optional caller capability: omission cannot impersonate an assignee. `last_transition` was already nullable; its nonempty strings became AssignmentChange declarations. Missing provenance never authorizes exact uncertain-reply retry. TodoStore invokes assign/transfer/release/state operations against the same row. |
| `queued_input.py:201,204,208`; `input_drain.py:98,428,504` | Optional after-clear/restoration returns select **bounded live queue/editor resources**, not cancelled/failed input state. InitialInput survives follow-up clear and is not restored after terminal completion. Awaited InputStarted publication precedes queue removal; original typed disposition owns started/UNKNOWN. |
| `acp.py`; `acp_extension.py`; `turn_runner.py`; `input_drain.py`; `queued_input.py` | Optional original input ID is **request/resource identity presence**. Existing ACPInputIdText creates/decodes it; absent native event ID resolves the retained original resource. Optional display text and pop defaults carry presentation/resource results, never sent/failed authority. |
| `goals.py`; `selected_participant.py`; `selected_turn.py`; `wake_injection.py` | No newly added absence selector. Original goal scalars/source and declared wake framing replace repeated checks; later #462 reply consumption is reviewed in its companion receipt. |

Existing inherited nullable relations remain visible, not renamed as external.
The exact transfer/release retry chains, duplicated retry predicates and
native-ID parser were deleted; no nullable status field, second assignment,
wait mirror, seen list or new storage format replaced them.

## Previously open native builder defect now corrected

The original #462 disposable-backup witness admitted a partial recorded cursor.
Arendt's **f997cc8586c1dd7551d85d0cbd7334fd215dabca** is present in `970`:
`native_input_record.py:84,148` validates complete recorded or wholly unrecorded
SQL groups; `native_runtime_input.py:189,296` declares their fields;
`cursor_owner.py:76` and `native_source_cursor.py:215` consume the declared
reference. Missing required generation is rejected, not passed as None into a
recorded reference. This is a concrete source fix, not an external-NULL excuse.

**Disposition:** no new None-as-domain-state in our traced #457 contributions;
no additional own source fix required. The former shared builder gap is closed
at this exact integrated source. Historic unrecorded-send/UNKNOWN debt is not
claimed closed. Original native proofs and prior installed receipts remain
protected; Einstein owns the current paired installed gate.
