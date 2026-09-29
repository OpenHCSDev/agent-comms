# Q8 journal transaction ownership: ready

991 production lines deleted/replaced;1111 added, including moved declarations and
role bodies (net+120). Source tested: `8ac16539b75577d92d8ad1ec6ece59275d191d9e`.
Current main integrated: `efcdf49377261a209fc94ae1b9f244d6032404cb` (through373/374).
PR376, owner Boyle. Parent owns merge, paired install and default/live activation.

## Ownership and deletion

The former CompactionJournal mixed every transaction role. It now owns only the
single database lifecycle, declaration-derived schema, BEGIN IMMEDIATE, rollback,
COMMIT and parent-directory fsync. Its former operation, enrollment, selected
attempt and publication methods are removed, with production and test callers
migrated directly to `operations`, `private_inputs`, `summaries`, and `publications`.
No facade entrypoints, compatibility reexports, codec subclasses or second store.

Plain module moves are not the factoring claim. The actual collapsed authorities:

- `SessionJournalHistory` replaces the hand-maintained three-table enrollment
  exclusion roster. Canonical TypedTable family discovery derives its members.
- `UnresolvedJournalHistory` derives unresolved membership from the table's cached
  declared state annotation, replacing five SQL predicate copies.
- `SelectedSummaryAttempt.transition` owns exact-record comparison, lifecycle
  legality and checked SQL update. Every selected transition uses it. Repeated
  identical refusal remains idempotent and blocking; a new SQLite regression
  preserves that behavior. Lifecycle state owns whether an original native start
  can retire the barrier; callers no longer probe its eligibility flags.
- `PrivateInputs` owns both raw prewrite reservation and the transaction retained
  through actual pipe write. Their exclusion check has one implementation.
- `NativeOperations.resolve` keeps native outcome and pending publication atomic
  on one connection. `CompactionPublications` owns exact observation, not native
  mutation or input authority.
- Shared error declarations break the former journal/identity/state import cycle;
  repeated local error imports are deleted. Runtime role dependencies point to
  the actual database owner, and record/state cycles are annotation-only.

Terminal receipt issuance remains exclusively after the verified terminal
transaction COMMIT and parent fsync return. Generic SQL/CAS does not mint a grant.
Fresh enrollment likewise requires the original returned creation object and
returned enrollment acknowledgment. Native/source/owner fences, exact matching,
wire-lock-through-write, immutable terminal outcome, UNKNOWN and no replay remain.
The SQL schema and durable payload shapes are unchanged; no converter/cutover.

Latest global AGENTS, NRA and refactor-audit were reread. Resolved SKILL and pattern
README were checked equal to the authoritative archive. Patterns: IMPL-12 shared
mechanics; MEMB-2 derived capability membership; IDEN-1 exact record identity;
IDEN-3 lifecycle-owned blocking; TIME-3 complete caller deletion; AGENT-6 moves
reported separately from factoring. Resource warning prohibited a global NRA
scan/new workers; bounded source dependency analysis and packaged guard used.

## Actual installed evidence

Noneditable isolated installation (`installed-location.txt`), normal immutable
native package `native-current-d3967e8b6ee0cf28`; localhost controlled provider
responses only. Native process, saved file, SQLite, ownership, transport and ACP
are real. No live root/default/launcher/package was changed.

- `focused-final.txt`: **94 passed,2 optional skips,13.76s**. Journal fsync/crash,
  two-process reservation, raw-write exclusion, selected lifecycle, enrollment,
  consumed/forged admission, native-start retirement and S9 guards.
- `current-main-native.txt`: **2 passed,22.94s**. Actual private ACP selected
  compaction -> native commit -> publication -> original prompt, repeated on the
  continued saved session; client observation asserts publication before exact
  original input binding, followed by exactly one native user entry/provider post.
  Disconnected selected child remains UNKNOWN without input replay or rebinding.
- `installed-native.txt`: **4 passed,33.94s**, before current-main integration and
  final idempotence correction: selected native summary/commit; private ACP handoff;
  correction after native commit refuses admission; disconnected UNKNOWN. The two
  affected continuous cases above were rerun on the final integrated production.
- `focused-stale-native-fixture.txt`: **105 passed,2 skips,1 failed**. Its eleven
  publication cases passed: client/socket incarnation changes, uncertain delivery,
  exact reprojection, cancellation and cross-process identity fencing. The sole
  failed case mocked a native stream behind an invalid `{}` saved session; current
  native preflight correctly rejected it. That obsolete test was deleted and its
  publication-before-original assertion moved into the actual native journey above.
  This receipt is not described as an all-green suite.
- `ratchet.json`: no increase. CompactionJournal excess beyond500 removed206;
  two foreign-state probes removed. Boolean chain terms, codec and raw reads do
  not grow. Actual journal82 lines; largest new transaction owner310 lines.
- `lint.txt`: Ruff F/E9/I passed. Diff check and constructor/receiver caller census
  found no former journal API consumer left, including direct constructor calls.
  S9 guard discovery includes all compaction role modules.

Commands: `uv venv .venv --python /usr/bin/python`, then noneditable
`uv pip install --python .venv/bin/python '.[acp]' pytest pytest-asyncio`.
Tests use `PI_COMPACTION_TEST_PACKAGE` pointing at the package above,
`.venv/bin/python -m pytest -o addopts='' --basetemp=.scratch/<case>`.
Focused files: test_compaction_journal, test_selected_summary_journal,
test_selected_summary_admission, test_fresh_private_session,
test_compaction_send_admission, round2/test_s9_guards.
Final native selectors in test_selected_owner_compaction_integration:
`test_acp_selected_summary_handoff_uses_final_prompt_once[summary-unchanged-private]`
and `test_disconnected_selected_summary_stays_unknown_without_original_replay`.

## Retained failures and remaining scope

Original first/correction receipts show missed reopened test callers and an old
error-text assertion, corrected without weakening the refusal. Missing-package
receipt records absent fixture environment, corrected before actual native tests.
Publication-stale receipt records consumers of the removed ACP metadata shape;
these now use the canonical decoder. No receipt was overwritten to hide failure.

No diagnosed remaining blocker in this role. CI deferred. Parent373 HistoryViews
and Wegener367 selected lifetime remain their owners;367 received the new caller
contract directly. Whole Q8/history/T4 completion is not claimed here.

`cleanup-owned.json`: no process references to own disposable roots;28MiB env and
43MiB test scratch plus caches removed. Source, failed/passed receipts and shared
immutable native package preserved. No native package copy or live history copy
was created.
