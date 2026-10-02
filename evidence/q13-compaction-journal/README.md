# Q13 CompactionJournal enrollment/publication closure — PR371

**213 production lines deleted;284 added across five modules.** The added71 lines
name previously implicit receipt/coverage/custody identities and put behavior on
existing record/lifecycle owners; they do not create another journal, registry,
codec, store, compatibility path or authority mechanism.

Source **06c74b5c0635c4c61be303c5bc4a7a3fac980cee**, merged current main
**976bad0f** including365 and369. Owner Boyle; parent owns merge/install/actual
live activation. Persistent tree `~/wt/comms-q13-compaction-journal-20260929`.

## Whole-role ownership and deletion

- `EnrolledPrivateSession.require_coverage` consumes the actual returned
  enrollment scope and owns its identity/admission checks. `FreshCoverageIdentity`
  uses the existing `ThreadIncarnation` and `ProcessIdentity`. The enrollment weak
  map retains the exact durable record with its journal path, replacing its tuple.
  O_EXCL object identity, header/inode verification, owner directory, native-history
  exclusion and successful post-COMMIT parent fsync remain required. Existing
  GenerationCounter/FieldCodec own positive exact-integer boundary decoding.
- `SelectedSummaryAttempt` owns its operation identity and lifecycle transition.
  `CompactionOperation` owns exact committed summary linkage and publication
  construction. `SelectedCommitReference` declares the selected portion of the
  native intent once; the commit producer encodes it and begin/link/admission
  callers consume it. Its projection derives field names from FieldCodec's
  declaration schema. Unrelated native witness/payload fields remain with their
  existing owner. This does not decode a union by guessed key shape.
- The two terminal transactions still mint only **after** their successful commit
  and parent fsync returns. Their weak-map scopes now retain `ReturnedSummaryTerminal`
  with the full exact terminal attempt, replacing five positional fields. Generic
  transactions and visible rows cannot mint a receipt. All issuer/consumer and
  forgery/fsync-failure test callers migrated; old signatures were deleted.
- `SelectedSummaryAdmission` no longer maintains duplicate path/session/operation/
  state/source/PID/inode fields. It holds the terminal record, original admission
  identity and `JournalCustody`, which binds path/inode and full ProcessIdentity.
  The token is burned **before** validation or durable input binding. Its existing
  identity owns current source/revision/text validation; the journal owns competing
  reservation/commit exclusion. Lifecycle verification receives the actual attempt,
  not a reconstruction from separate fields. No uncertain input is replayed.
- `CompactionPublication` owns exact metadata/operation agreement. Committed native
  evidence constructs the expected complete publication and record equality proves
  it. `NativeCommitPosition` is the shared entry/revision/leaf declaration inherited
  by the native outcome and published metadata; TextDigest owns digest validation.
  Deleted free `_publication_metadata`, duplicated position fields, tuple receipts,
  nine opaque long conditions and the retired callers. Constructor callers migrated.

**No durable schema or serialized-format change.** Current intent keys, sorted
publication bytes, source JSON and journal rows remain in place. No cutover tool
or old/new reader is needed. The journal's publication size/exclusion bounds and
SQLite EXTRA/DELETE/BEGIN IMMEDIATE/directory-fsync behavior remain intact.

Patterns applied: **IDEN-1/3/8**, **BOUND-1/2**, **IMPL-5/12**, **TIME-3/9**.
Latest NRA and refactor-audit guidance was reread; installed refactor-audit SKILL
was byte-compared with the authoritative `.skill` archive. Read round2 rules and
the new main AGENTS/owner decisions. Resource assertion stayed warning (root6.5GiB,
home12.3GiB, RAM14.4GiB, swap9.0GiB): bounded serial cases, no global scan, agents,
paid provider calls or live-state edits. This is an authored semantic refactor;
no NRA DSL equivalence proof or exhaustive whole-plan completion is claimed.

## Actual affected-path evidence

Tests import a **noneditable installed package**, not the worktree source:
`~/wt/comms-q13-compaction-journal-20260929/.venv/lib/python3.14/site-packages/agent_comms`.
Pinned native:
`/home/ts/.local/share/agent-comms/native-current-d3967e8b6ee0cf28/node_modules/@earendil-works/pi-coding-agent`.
The existing native/ACP fixture uses retained native history and controlled
localhost provider responses; native processes, sessions, journal, SQLite, commit,
input binding, transport and send fences are real.

| Receipt | Observed result |
| --- | --- |
| `current-main-installed.txt` | **65 passed,2 optional native skips in19.72s**, after main369 FieldCodec/TypedTable integration: journal, terminal-admission, fresh-file enrollment, ACP metadata contracts plus actual private saved-session summary -> native commit -> original ACP prompt handoff. |
| `installed-native.txt` | **3 passed41.83s**: actual selected summary/native commit and ordinary/private ACP handoffs; exactly one original accepted, one compaction, pending local publication and correct original binding.14 other parameter combinations deliberately deselected. |
| `native-refusal-unknown.txt` | **2 passed9.34s**: real native commit followed by a durable correction cannot issue admission; actual selected child stopped after its request reached localhost stays UNKNOWN, source bytes unchanged, no original binding and one request only. |
| `boundary-closure.txt` |44 passed1.84s after receipt/metadata changes: malformed metadata refusal, fsync loss, immutable terminals, token forgery/one-use/process death/source drift and external ACP representation. |
| `focused-first.txt` |62 passed,2 optional native skips2.02s at first implementation checkpoint. |
| `ratchet.json` | No increases: **50 chain terms,9 long conditions,5 foreign absence probes and5 type-identity checks removed**; class excess above500 decreases38. Zero long conditions remain in journal, state, identity and selected-admission modules. |

The initial native run failed three cases with UNKNOWN committed outcomes while
my uncommitted version was missing the TextDigest import (also caught by Ruff).
The import was corrected; the original RED receipt is retained in
`installed-native-first.txt`. Fresh isolated cases passed afterward. Failed
journals were not cleared or replayed to make a test pass.

Commands: `python -m pytest -o addopts=''` with
`PI_COMPACTION_TEST_PACKAGE` set to the package above. Final installed selection:

```
tests/test_compaction_journal.py
tests/test_selected_summary_admission.py
tests/test_fresh_private_session.py
tests/test_acp_extension.py
tests/test_selected_owner_compaction_integration.py::test_acp_selected_summary_handoff_uses_final_prompt_once[summary-unchanged-private]
```

Ruff F/E9/I and diff whitespace checks pass. Retired scope fields/free publication
helper/caller signatures were checked directly. Native writer locks, original
input reservation checks, exact source digest, immutable terminal state, raw
UNKNOWN exclusions and post-fsync minting remain mandatory.

## Handoff

No diagnosed source/caller blocker remains for this Q13 slice. **Ready for parent
integration and live verification; not installed live by this worker.** Native
package/pins are unchanged. ParentA2, Wegener PiEvent/fresh-native and Tesla366
retain their scopes; no competing implementation or additional coordinator.
CI is deferred. Broader plan closure remains tracked separately.

Owned installed environment and caches removed after process-reference check;
source and all receipts retained (`cleanup-owned.json`).
