# Original372 native RPC preflight receive

Owner: Arendt. This draft examines the original merged-models and
merged-boundaries failures `0884315c2eb20a75054e8a49d98a19f1` and
`087471776b85cd8040b9e94bf2db0d25` before prompt admission.

Trace the existing native launch, initialization, get-state command, state
construction, response writer and Python reader owners. Read original diagnostic,
acquisition and process evidence before changing their shared behavior. Preserve
the original NotSent receipts, saved journals and uncertain attempts. No provider
calls, original-input retries or deadline changes belong to this investigation.

Mendel530 retains Python proof decoding and phase/source publication. Einstein
retains optional compaction policy; native producer changes are coordinated with
Einstein and Sch. Completed529 remains separately merged and qualified.

Current evidence establishes an unanswered get-state control command, empty
retained stderr and no prompt admission. It does not yet identify whether the
original native child was still initializing, constructing state or writing its
response. Source closure precedes final proportionate validation.

## Source finding and change

Original native5184 restores the saved session before installing RPC's stdin
listener. `AgentSession` opens the entry store, recovers the existing native
proof journal and restores `SessionContext`; RPC then binds extensions before
attaching its JSONL listener. Once dispatched, `get_state` exhausts
`storedContext.messages()` just to count messages. For `CompactionContext`, this
walks the selected retained entries and decodes their payloads again. Only then
can the response enter `output-guard`'s existing ordered stdout writer.

The change uses the existing owners:

- `sessionEntryToContextMessages` remains the single entry-to-message projection.
  `EntryMetadata` captures its cardinality during the original index decode.
  This is a selector in the existing disposable index, not input admission or
  a second context/budget authority.
- `EntryStore.contextMetadata` owns the original compaction-aware selected
  branch algorithm. Both body iteration and counting consume it. The replaced
  body-dependent selection algorithm is deleted. Counting retains the original
  disk revision checks before and after the observation; memory storage supplies
  its own existing resource validity hook.
- `ReadyContext.messageCount` uses its resident agent messages;
  `CompactionContext.messageCount` uses the entry selectors. RPC asks that owner
  instead of exhausting decoded bodies.

Original session/proof bytes, native admission, compaction policy, payload budget,
Python watchdog deadlines and the external get-state response shape are unchanged.
No fallback reads an old selector: each disk reader creates its own index from
the original session. Constructor, append, memory and disk paths share the same
`EntryMetadata` producer. `contextEntries` consumers keep their selected ordering,
including the latest compaction followed by its kept suffix and subsequent rows.

## What the original clocks establish

The original failure streams are named by lease, not terminal diagnostic ID:
`a02b021b…requests.jsonl` and `f53393a2…requests.jsonl`.

| Operation | Models | Boundaries |
| --- | ---: | ---: |
| Startup slot | 0.091 ms | 0.066 ms |
| Spawn | 20.197 ms | 20.243 ms |
| Get-state send | 1.059 ms | 0.583 ms |
| Get-state receive | 19.999911 s | 18.003809 s |

Both original receipts are **NotSent**, before prompt admission. Their streams
contain no native request-progress record or native process identity. Native
startup timing is disabled unless explicitly enabled and prints after runtime
construction; it supplies neither handler-entry nor response-flush timestamps
for these attempts. Empty stderr cannot distinguish unfinished initialization,
handler work, stdout delivery or a delayed Python reader.

Removing the count's payload decode is a source-proven cost deletion. It is
**not proof that counting consumed the entire historical timeout**. Context
restoration, proof recovery and extension binding retain their separate work.
Historical per-operation timing cannot be reconstructed from the retained
artifacts. Mendel530 independently owns Python publication/proof costs.

## Receiving requirement

Draft source checkpoint only: the native manifest and a matching freshly built
package must be received through Sch's existing builder before qualification.
Final validation needs one affected native get-state read on representative
retained history, count/selection agreement and revision refusal. No provider
input, original attempt retry, deadline extension or public restart is needed.
Completed529 resources and the two original372 receipts remain protected.
