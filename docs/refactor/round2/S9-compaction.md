# S9: Compaction

**Head audited:** `agent-comms` `main` at `a10655d`; re-verify at yours. **Rules:** [00-RULES.md](00-RULES.md). **Builds** [A14 `PiHelper`](02-SHARED-ABSTRACTIONS.md#a14-pihelper); **uses** A2, A12 (after S13), A13 (after S12).
**Steps 2 and 3:** K1, K2, K5 and K6 start as soon as step 2 opens; K3 waits for A13, K4 for A12.

## Current implementation — PR236

K1–K6 source migration implemented in PR236 with shared A12/A13/S10 owners.
Local authority transport/launcher/watchdog and startup keysets are deleted.
All three S9 guards pass. Actual native authority36cases pass; two shared
acceptance repairs remain: current ThreadManagement ProcessIdentity caller and
S10 acceptance of external saved parent UUID IDs. Full receipt/deletion map:
[evidence/s9/HANDOFF.md](../../../evidence/s9/HANDOFF.md).
Parent owns quiet journal reset and actual activation; neither is claimed here.


---

## Already done; do not rebuild

#186 built the compaction lifecycle: `OperationState`, `SummaryState` and `PublicationState` on the shared lifecycle base, with state-specific data and a `TerminalOperation` capability composed by multiple inheritance. `compaction_journal.py` has no string state comparisons left.

The `owner_compaction_*` modules are pipeline roles in sound layers (policy, orchestration, transaction, journal). Leave that layering alone.

**Manual and owner compaction are different operations** (D21): manual compaction is pi's own `/compact` on a saved session file, with pi's session file as the record of truth; owner compaction is agent-comms' journaled transaction with an authority child. Do not merge them.

---

## What remains, and what gets deleted

**K1. pi's compaction settings, restated six times.** Keep exactly one: a `PiCompactionSettings` A2 record with wire names for `reserveTokens` and `keepRecentTokens` and agent-comms' own bounds, with `PiCompactionDecision` composing it and its `trigger`. **Delete:**
- the second class declaring the same fields in `pi_summary_payloads.py`;
- the hand-built dict literal in `owner_compaction_adaptive.py`;
- the JavaScript re-validation and rebuilding in `owner_compaction_settings.py`, `owner_compaction_prepare.py` and `selected_source_snapshot.py`; helpers receive already-validated settings as input;
- **the hard-coded `16384` and `20000` in `manual_compaction.py`.** Those are copies of pi's defaults, and pi owns them. Check whether pi merges the `compaction` section of its settings with its defaults: if it does, write only `"enabled": false`; if not, read pi's defaults through a helper. Either way, those numbers leave the Python code.

**K2. JavaScript embedded in Python strings,** about 9,900 characters across `selected_source_snapshot.py`, `owner_compaction_settings.py`, `owner_compaction_prepare.py`, `native_session_reopen.py` and `manual_compaction.py`. Code in strings is invisible to every tool. Move each helper into a `.mjs` file shipped as package data, run it through A14 with A2 request and result records, and **delete the strings.**

**K3. Tables read by column name.** The journal's five tables (`operations`, `publications`, `selected_summary_attempts`, `private_raw_inputs`, `enrolled_private_sessions`) and the session tables in `fresh_private_session.py` and `continued_private_session.py` move to A13. **The journal is runtime state and is reset at cutover**, which happens with no compaction in flight.

**K4. Child processes.** Manual compaction's own supervision (`_signal_group`, `_group_alive`, `_shutdown`) and the authority child's watchdog move to A12. **Delete** the local implementations S13 lifted, and `compaction_child_launcher.py` if nothing imports or runs it.

**K5. Hand-written exact key-set checks** (eight in S9's files) disappear with A2's strict decoding.

**K6. Re-validation:** confirm that the 12 `type()` checks in `selected_summary_admission.py` and the 8 in `owner_compaction_commit.py` re-check values their types already guarantee, then delete them.

Delete any legacy or compatibility code in S9's files on the way (rule 1).

---

## Guards

In S9's files: no `subprocess` or `os.kill` calls (A12 only); no JavaScript in Python strings; no exact key-set checks; no reads of a row by column name; no restatement of pi's compaction settings outside `PiCompactionSettings`.

---

## Tests

- **One integration test per helper against the pinned pi package**, strictly decoding its real output. That tests the helper's contract with pi, which is external. There are no golden files for the helpers' own JSON, which is ours.
- **One new-case test for A14** (T2): a test-only helper declaring only its script and records.
- **One contract test for what agent-comms writes into pi's settings,** since pi owns that format.
- **Delete** the tests of the embedded strings, the hand parsers, the second settings class and the local supervision code. Do not port them.
- The compaction behaviour tests keep passing.

---

## Done when

K1 to K6 are complete, the guards pass, the JavaScript strings are gone, the journal has been reset at cutover, and nothing in S9's files supervises a child or decodes a record by hand.

## Dispatch

> **`refactor-s9`:** Complete S9 per `docs/refactor/round2/S9-compaction.md`. Read `00-RULES.md` first. The lifecycle already landed; do not rebuild it, and keep manual and owner compaction separate (D21). Start with K1, K2, K5 and K6; take K3 once A13 lands and K4 once A12 lands. Delete as you go.
