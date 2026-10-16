# Goal history: indexed reconciliation and owned selections

PR #958. Current main through `c5623efa` is integrated. #966 is merged; its stopped-worker installation remains Codex's work. No live journal, shared installation or uncertain input was changed here.

## The six requested corrections

1. `goal_history_state.py`: `GoalHistoryState` declares `Pending`, `Committed`, `Aborted`, `Uncertain`. FieldCodec encodes the class references into the existing durable labels. `Pending.reconcile()` owns the pending outcome decision; settled members inherit the no-op. The table column and state queries use this family, not copied state strings.
2. `GoalHistoryStore._rows()` requires a state. Observation reads every indexed pending row, then the latest committed row. There is no optional-state SQL splice.
3. `GoalHistoryRead` has `NoHistory` and `ThroughSequence`. NoHistory never opens a database; ThroughSequence owns the captured path/incarnation/positive sequence. A committed clear is ThroughSequence, not NoHistory.
4. `TypedTable.select_statement()` and `select_union()` own SELECT construction. `select()` and `stream()` use the public builder. Goal selection no longer imports `_identifier`, reads `_column_list`, calls a private query helper, or assembles SELECTs. Schema inspection still queries SQLite's catalog. The existing `_condition()` owns predicate encoding for both reads and claim deletion.
5. History I/O translates only `OSError` and `sqlite3.Error`. ValueError and TypeError decoder defects propagate unchanged from page reads, report lookup, original read-only acquisition and observation.
6. `GoalHistoryQuery` owns `scope=AllGoals()/OneGoal(id)`, `cursor=Newest()/Before(sequence)`, and `limit`. FieldCodec owns nested request/tool encoding; scalar `goal_id`/`before_sequence` arguments are rejected, not silently accepted.

Example:

```python
query = GoalHistoryQuery(scope=OneGoal(goal.id), cursor=Before(sequence), limit=7)
payload = FieldCodec.encode(query)
```

Core requests, native tools, tests and the one-shot report-retirement tool's state consumers are migrated. The paired Toad implementation owner is not confirmed; Codex must assign one. There is no compatibility path.

## Preserved behavior and bounds

- Reconciliation decodes at most P+1 entries for P pending intents. Every pending row remains available; there is no unique-pending constraint or display-budget clipping.
- Display pages are newest-first, at most 100 entries. This is not journal retention or recovery policy.
- Both replacement endpoints match OneGoal. Each endpoint gets an indexed, independently bounded projection; TypedTable performs the unique union and final limit.
- Report lookup is an exact indexed query, independent of the display page.
- Registration captures the read cut under its lock and decodes outside it. Later appends cannot extend that cut; another process can acquire the registry lock during decoding.
- Commit/abort/uncertain outcomes, committed clear versus no history, aborted history and old reports beyond 100 entries are retained.

## Performance

Actual Registration transitions, registry lock and fsync; three samples per authored journal size. No provider or live bus. Original baseline is `127465dd`; final measurement uses the current-main-integrated product at `d759ec41`.

| Settled H | Original median ms | Integrated candidate median ms | First-transition decodes |
|---:|---:|---:|---:|
| 100 | 25.33 | 19.06 | 200 → 1 |
| 1,000 | 107.21 | 19.03 | 2,000 → 1 |
| 10,000 | 789.27 | **19.27** | 20,000 → 1 |

Earlier family-only measurement was 17.74 ms at H=10,000; the table uses the later integrated measurement, not the faster earlier result. B-tree, pending-row, fsync and scheduling costs remain.

## Verification

- Source: 40 focused checks passed.
- Current-main private file-wheel installation: **42 passed, zero skipped, 34.35 s**. Includes actual immutable Pi child/owner/CLI lifecycle using controlled localhost provider responses.
- Native nested goal-history call: one fresh input, one actual checked tool call with OneGoal/Before, exact retained row returned, two provider requests, original authored session prefix preserved, no diagnostic, child retired.
- Existing Send/Bash native journey also passes with the real async admission awaited: one message, one file effect, one reply; no input/effect replay.
- Installed receiving ratchet against `c5623efa`: exit 0, no positive debt delta; exact previously verified audit wheel at `120c04fb9a0eb4afafab6d621d1c5fd421bb7552`. No waiver.
- Private installed CLI: original 10,003-row journal refuses missing indexes; read-only plan preserves bytes; applying four indexes preserves every row. Disjoint seven-row pages, default 100, OneGoal filter, real append, invalid-limit refusal, legacy-argument refusal and all 105 pending outcomes passed. Its nine goal-related production files remain byte-identical in the integrated wheel.
- State-family conversion leaves every table/index DDL string byte-for-byte unchanged. Source, wheel and installed goal modules match.
- These are not shared/default installation, configured paid-provider acceptance, or physical Toad UI acceptance.

## Structural evidence and limits

Reused NRA's parser/single-traversal index across Core production/tests/tools, paired Toad production and the actual installed metaclass-registry dependency. Final inventory: 1,071 modules, no parse omissions. Declarations, inheritance, imports, writes, decisions and consumers are retained in `family-before-ast.txt` / `family-after-ast.txt`.

Coverage correction: the first family inventory derived a nonexistent dependency path from this checkout's absent venv; it did not inventory that dependency. The reused driver now takes the actual dependency root explicitly and refuses missing roots. The final scan includes the published dependency actually imported by the private installation. Installed MRO/schema/codec, SQL plans, cross-process locking and native execution separately confirm dynamic paths; syntax alone does not resolve arbitrary getattr, callbacks or lazy registry loading.

Removed owner paths: optional retained sequence, optional scope/cursor scalars, optional-state SQL splice, copied literal states, private SELECT construction and broad decoder-error translation. The remaining Literal entry kind describes transition/baseline/gap, not journal state.

## Integration, cutover and remaining gaps

- Codex owns current-main integration/default installation. Keep #966's stopped-worker claim carry and UNKNOWN-launch handling intact. Its `_condition()` already serves the public SELECT builder and deletion; do not restore `_query` or a second predicate compiler.
- Existing goal journals need **four nonunique indexes only** through `tools/cutover/goal_history_indexes.py`, default read-only plan, reviewed apply with old strict writers stopped. Do not use the superseded five-index/unique-pending proposal. No goal rows/table/state labels are rewritten.
- **Paired Toad migration blocks merge/install.** Toad-close-lifecycle explicitly retained #677 only and never claimed or edited GoalDetails; the earlier ownership handoff was wrong. Codex must assign/confirm a paired implementation owner for nested queries, newest-first/older-page modal behavior and isolated physical UI acceptance. The existing reverse/full-history consumer must migrate in the paired change; otherwise ordering is wrong and history is clipped to one page. Do not install this standalone wheel over newer main work.
- Separate pre-existing defect: `thread_format_retirement.py` still accesses the removed `Thread.last_goal_report_turn` carrier member. Its earlier 11 preservation failures were reproduced on the untouched baseline; this change migrates its state comparisons but does not claim its carrier contract is repaired. Codex was asked to assign that repair; those failures are not included in the 42-pass selection.

## Retained evidence and scratch ownership

Owner: core-goal-retry. Purpose: #958 implementation, private acceptance and comparison. Root:
`/home/ts/.cache/agent-scratch/core-goal-retry/goal-history-tail`.

Current runtime: `runtime-families`; current wheel: `wheels-families-main/agent_comms-0.1.0-py3-none-any.whl`. It borrows unchanged dependency files from the published `toad-2885cddb9-core-e7a08b454-native-6468a1979d983e9d-20261016T065703Z` runtime and the existing development pytest environment. Retain that dependency runtime while borrowed.

Concise results: `family-main-installed-primary.txt`, `families-main-installed-after.json`, `ratchet-families-main-installed.{json,err}`, `family-main-installed-receipt.json`, `installed-families-result.json`, `family-before-schema.json`, `family-after-ast.txt`. Earlier family-only receipts remain labeled separately. Authored native histories/inputs are preserved; both native child processes retired. Retire completed disposable benchmark/build output only after acceptance and actual borrower checks.
