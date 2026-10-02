# Selected summary recovery through original source custody

Arendt owns summary reservation/recovery in this checkpoint. Einstein owns #520 task-aware decision policy and optional-decline rendering; Singer owns #521 retained inspection. Source reasoning and coherent caller migration precede end validation.

## Actual blocker

Public c601 preserved operation `371c6e50b7f94e9f904b953b290ad6ee`: a correlated `RefusedSummary(context_requires_compaction)` from turn `63586c52b41b4390942b314cc6ff23c6`. Its sole original `acp:bb30216393204b2b9a69c52514802170` is `NotSentInput`, with no native binding. Both original native and input-proof file revisions still match the reservation exactly. No native commit operation exists for this session. Current activity records `stopped_drain/CompactionJournalError/Blocked selected summary`; current owner is idle. This is a retained known refusal, not evidence of a new provider failure.

## Existing owners and closure

- `SummaryState` owns reservation/terminal/recovery semantics; `SelectedSummaryAttempt` owns the original request and exact CAS transition. Storage state is not input admission.
- `SelectedSource` / `SelectedAdmissionSource` own original incarnation, turn, revision and input membership. `InterruptedReservationCheck` / `InterruptedInputCheck` own source equality and the prohibition on retiring a native-bound original.
- `CompactionBoundary.hold` acquires native writer, current owner/turn, wire and input custody; `OwnerCompactionCommit` uses it before preparing a new summary.
- `SelectedSummaries` owns durable transition and exact commit exclusion. Successful link/clean-decline post-fsync acknowledgements remain the only original-input admission issuers.
- Previously interrupted UNKNOWN reconciliation used original source checks while manual refused reconciliation independently checked incarnation/reservation flags; ordinary preparation did not reconcile known refusal. All three consumers now use the existing state/source recovery behavior, with manual duplicate decisions and callbacks deleted.
- Reservation, pre-turn/manual preparation, native commit/link, input admission, and read-only outcome projection remain consumers of those owners. Historical UNKNOWN is not reclassified or replayed; retirement can only remove an exclusion after original no-write/source evidence, never bind/send the old input or resume its goal.

No public mutation, input replay, provider call or restart has occurred. End validation below exercised the affected installed original lifecycle after the source change; unrelated qualified 506/516 gates did not repeat.

## Implemented owner change

The existing `SummaryState.retire_unchanged_source` hook now describes every no-write recovery case: an interrupted reservation retains UNKNOWN, an observed UNKNOWN retains UNKNOWN, and a known refusal retains its refusal reason. Other states return themselves. The journal requires the original source check, exact row CAS and absence of any matching native commit intent before transitioning. It never returns an original-input admission token.

The existing bridge uses the same writer/owner/input boundary for all these states. Removed the separate recovery flag, manual-only state method, unguarded `retire_refused`, manual incarnation/pending-input reconstruction and `before_summary` callback. `settle_selected_refusal` provides #520 a current-source effect with original commit/reservation checks; this is distinct from older-turn recovery and admits no input.

Current additional manual reservations: open-prs `824b080a`, merged-runtime `3dcf9c47`, merged-models `03edf691`. Their first recorded failure is `Compaction source changed; derive fresh evidence`, followed by the persistent reservation exclusion. Both native **and input-proof** identity, size, mtime and ctime remain exactly at their reserved revisions. Owners are idle and no native commit exists. The full pre-summary capture also includes settings, ingress, goal and retained facts; the historical exception did not retain which of those differed, so this receipt does not invent that cause or ignore proof changes. These are interrupted no-write reservations; a state-only terminal claim is insufficient. The held source checks must succeed before any retirement, and subsequent work must use a distinct authorized input.

Source census: `before-consumers.json` uses existing NRA Package/ParsedModule over 311 production modules, zero parse omissions; static references are candidate callers, with source semantics read for all listed recovery/commit/admission consumers. SQL stores and immutable source_json remain unchanged; lifecycle successors reuse existing serialized states, so no journal DDL or native schema change is introduced.

## Installed end validation

Functional source is `30f9cf74f6f1ede7d4f795ea857cce51b6e7857c`; subsequent publication only adds tests and evidence. Four production files delete 53 lines and add 63. `after-consumers.json` records the current declarations and references, including input/commit exclusion consumers; removed manual recovery flag/method/callback and unguarded refusal method have no production consumers.

`installed-recovery-receipt.json`, raw `/home/ts/wt/s52303/receipt.json`: **PASS 17.280 seconds**. A normal installed wheel matches the complete checkout source. Read-only capture after the 316 restart validates all four actual original reservations against their current incarnation/old-turn fences, exact native and proof revisions and original input membership. An SQLite backup receives the exact original CAS transitions, retaining each original request and granting no original-input admission. This does not mutate the public journal.

The real SDK forks the 42,044,804-byte architecture session. Native5184 prepares the 42,044,813-byte private fork with the configured `openai-codex/gpt-6.1-sol` / HIGH and automatic global extensions. The original bridge then performs held reserved, UNKNOWN and refused recovery on that fork: future admission exclusion opens, original inputs and native bytes remain unchanged, and the acquired native child closes. No prompt is written and no provider input is sent. This qualifies installed recovery/preparation, not a new provider reply, a public recovery, or the complete joined 520 UI workflow.

Failed run01 remains visible at `/home/ts/wt/s52301/receipt.json` and the checkout `.artifacts/s523-installed01.log`. The original error lacked its first event. A no-input observation on the preserved fork decoded `UnknownPiEvent` carrying `extension_error/session_start`: the private root inherited public `PI_AGENT_ID=openhcs-architecture-memory` although its declared owner was `recovery523`. The driver now obtains the private environment from `Thread.native_environment` and model/effort from `NativeArguments`, as production does. Attestation is unchanged. Run02 reached successful native preparation but failed on absent-file bookkeeping and receipt serialization; its raw log is retained, not labeled a product failure or success.

One focused sanity batch: **12 passed, 2 native-package skips**; the additional existing `test_compaction_result` extension fixture fails because its sample subclass lacks already-required `adaptive_result` and `require_prepared`. Those production methods are unchanged from main; this is not reported as a green suite. The affected real installed recovery above passed. No optional broad suite or repeated provider attempt was run.

## Joined checkpoint and publication

Einstein #520 normally merged the fresh refusal API and owns the configured task-aware continuation gate. Sch #522 owns the single native artifact. #523 changes no native bytes, journal format, durable input format or public runtime. Parent owns any later paired installation and fresh authorized recovery; no original UNKNOWN input, failed goal or summary request is replayed here. Public original paths and failed private receipts remain protected; `.artifacts/s523-runtime` is this owner's small disposable validation installation, and `s52301..03` are private no-prompt fork evidence.
