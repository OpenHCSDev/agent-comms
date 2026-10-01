# Typed selected-summary journal fixture contribution

Integration owner remains Schrodinger #494. Kepler owns tests only in this
persistent isolated worktree. Base f6e70a15 (production source e0b15552), stacked
on #494's authoritative branch, with no production edits or functional-release hold.

Receiving handoff: tests/test_selected_summary_journal.py,
tests/test_fresh_private_session.py, and necessary existing fixture builders.
Read existing SelectedSummarySource, NativeIntent, OwnerCompactionAttestation,
CompactionSource, NativeWitness and NativeOutcome before changing fixtures.
Use these original declarations, preserve durability, exact-ID linkage and
UNKNOWN/no-replay assertions, and remove impossible retired API shapes rather
than keeping a compatibility adapter. No broad port or suite rerun.

Order: source contract analysis, coherent bounded fixture conversion, one
provider-free affected sanity batch at the end. No native/provider/public call.
The existing large retained manual/adaptive installed gate belongs to Sch and
continues independently. This contribution does not establish product readiness.

## Scoped handoff

Implementation checkpoint: `765b55ba`. Production edits: zero. The three test
files replace 38 lines with 117 lines using the existing source, intent, witness,
attestation and outcome declarations. No new family or compatibility adapter.
The shared `native_intent` fixture returns `(intent, owner, source, selected)`;
its optional reference is the caller's original selected operation unchanged.

Final affected provider-free batch: **34 passed in 5.65s**. This ran
`test_selected_summary_journal.py` and the four changed private enrollment/floor
controls in `test_fresh_private_session.py`, serially with empty pytest addopts.
The copied-native opt-in environment variable was unset. No native/provider or
public operation ran. Reused interpreter:
`/home/ts/wt/comms-retained-context-framing-20261001/.artifacts/retained-context-framing-20261001/runtime/bin/python`,
with this worktree's source/tests on `PYTHONPATH`.

Raw result: `.artifacts/typed-journal-fixtures/provider-free-batch01.log`.
Resource warning was handled with one bounded 60-second batch, no new environment
or parallel fixtures. Sch retains functional installed manual/adaptive ownership;
remaining old callers in other fixture files are outside this bounded contribution.
