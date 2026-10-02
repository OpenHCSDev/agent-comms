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

## Implemented owner change

The existing `SummaryState.retire_unchanged_source` hook now describes every no-write recovery case: an interrupted reservation retains UNKNOWN, an observed UNKNOWN retains UNKNOWN, and a known refusal retains its refusal reason. Other states return themselves. The journal requires the original source check, exact row CAS and absence of any matching native commit intent before transitioning. It never returns an original-input admission token.

The existing bridge uses the same writer/owner/input boundary for all these states. Removed the separate recovery flag, manual-only state method, unguarded `retire_refused`, manual incarnation/pending-input reconstruction and `before_summary` callback. `settle_selected_refusal` provides #520 a current-source effect with original commit/reservation checks; this is distinct from older-turn recovery and admits no input.

Current additional manual reservations: open-prs `824b080a`, merged-runtime `3dcf9c47`, merged-models `03edf691`. Their first recorded failure is `Compaction source changed; derive fresh evidence`, followed by the persistent reservation exclusion. Native files remain at their reserved revisions, owners are idle and no native commit exists. These are interrupted no-write reservations; a state-only terminal claim is insufficient. The held source checks must succeed before any retirement, and subsequent work must use a distinct authorized input.

Source census: `before-consumers.json` uses existing NRA Package/ParsedModule over 311 production modules, zero parse omissions; static references are candidate callers, with source semantics read for all listed recovery/commit/admission consumers. SQL stores and immutable source_json remain unchanged; lifecycle successors reuse existing serialized states, so no journal DDL or native schema change is introduced.
