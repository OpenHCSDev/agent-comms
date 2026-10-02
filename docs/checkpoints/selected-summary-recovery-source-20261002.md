# Selected summary recovery through original source custody

Arendt owns summary reservation/recovery in this checkpoint. Einstein owns #520 task-aware decision policy and optional-decline rendering; Singer owns #521 retained inspection. Source reasoning and coherent caller migration precede end validation.

## Actual blocker

Public c601 preserved operation `371c6e50b7f94e9f904b953b290ad6ee`: a correlated `RefusedSummary(context_requires_compaction)` from turn `63586c52b41b4390942b314cc6ff23c6`. Its sole original `acp:bb30216393204b2b9a69c52514802170` is `NotSentInput`, with no native binding. Both original native and input-proof file revisions still match the reservation exactly. No native commit operation exists for this session. Current activity records `stopped_drain/CompactionJournalError/Blocked selected summary`; current owner is idle. This is a retained known refusal, not evidence of a new provider failure.

## Existing owners and closure

- `SummaryState` owns reservation/terminal/recovery semantics; `SelectedSummaryAttempt` owns the original request and exact CAS transition. Storage state is not input admission.
- `SelectedSource` / `SelectedAdmissionSource` own original incarnation, turn, revision and input membership. `InterruptedReservationCheck` / `InterruptedInputCheck` own source equality and the prohibition on retiring a native-bound original.
- `CompactionBoundary.hold` acquires native writer, current owner/turn, wire and input custody; `OwnerCompactionCommit` uses it before preparing a new summary.
- `SelectedSummaries` owns durable transition and exact commit exclusion. Successful link/clean-decline post-fsync acknowledgements remain the only original-input admission issuers.
- Today interrupted UNKNOWN reconciliation uses original source checks; manual refused reconciliation independently checks incarnation/reservation flags and retires a row. Ordinary preparation never reconciles known refusal. Close these three consumers through the same existing state/source recovery behavior; remove manual duplicate decisions and callbacks.
- Reservation, pre-turn/manual preparation, native commit/link, input admission, and read-only outcome projection remain consumers of those owners. Historical UNKNOWN is not reclassified or replayed; retirement can only remove an exclusion after original no-write/source evidence, never bind/send the old input or resume its goal.

No public mutation, input replay, provider call or restart has occurred. Final validation will exercise the affected installed original lifecycle after the source change; unrelated qualified 506/516 gates will not repeat.
