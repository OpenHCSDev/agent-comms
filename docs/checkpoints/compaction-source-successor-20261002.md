# Compaction source and preserved facts

Owner: Arendt. Parent owns publication. Source base: Core155b0007.

## Actual source findings

The original #openhcs365 inputs join through the original acquisition records to four failed turns: open-prs b240, runtime5d816, models ca6, architecture2a94. All four native inputs remain unadmitted. None of those turns has a native commit intent. The later four successful manual commits belong to e079, c64b, 5659 and6eed; they do not establish recovery of365.

Original selected-summary requests retain their typed task payload. Projecting those payloads through the same FieldCodec journal view gives exact equality with each later committed intent's retained payload (9,9,9,43 facts). The complete failed capture was not persisted before commit begin; no later intent is substituted for it.

The actual certified wire prefix365 and its successor366 differ only by the new helper reply. For all four owners the current message-wide bus digest changes while the retained wire task facts remain exactly equal (8,8,8,13 facts). This demonstrates a competing question in source equality: addressed ordinary replies invalidate preservation although they add no preserved task fact. It does not recover the missing original failure capture or establish that no other field changed.

## Ownership change

Use the existing CompactionSource and RetainedTaskFacts. The held boundary remains the sole capture: original wire and input declarations, native branch artifacts, owner goal and applicability. Delete the separate message-wide bus digest from capture/equality and its original WireLog producer/callers. The source still fences native session/revision/leaf, root, owner epoch/turn, goal, original input projection and settings, and compares the full typed retained payload including original authors and references. Changed task facts must still refuse commit/admission.

Preparation changes only the original cut allocation. Native commit changes only the witnessed native successor. Manual summaries link a commit without admitting an input; adaptive summaries still recheck the full original source under writer/owner/input custody. No field is refreshed to make equality pass. Source refusal reports only differing declaration names, never private text or a new grant.

## Delivery

Source reasoning and complete caller migration precede validation. Original365 and every UNKNOWN remain untouched. This draft does not claim the installed application repaired.

## Stored intent and recovery consumers

Removing `bus_revision` changes the **future source audit projection** inside `NativeIntent.journal_json`. It is not a claim of identical persisted JSON shape. The source view is already a redacted projection: `native` is excluded and `native_json` is descriptive text. No product or cutover consumer decodes that view into `CompactionSource` or reconstructs an admission from it. Existing records keep their entire original audit view and bytes; this change performs no carry, rewrite, reset or legacy decoding.

The existing readers consume the same declared contracts:

| Producer or consumer | Original contract retained |
| --- | --- |
| `NativeOperations.begin` / `NativeIntent.journal_json` | Reserve once; store declared native request controls alongside the source audit view and optional selected-summary reference. Future audit views omit the removed field. |
| `NativeIntent.read` | Decode only this declaration's witness, payload digest and metadata digest. Do not decode the containing source projection. |
| `OwnerCompactionCommit.reconcile` | Compare the complete original `intent_json` byte string, then send only the declared reconciliation request under current native/owner custody. Never resummarize or retry an UNKNOWN provider attempt. |
| `OwnerCompactionCommit.admit_selected_original` | Require the original intent witness, exact stored operation, committed native successor and freshly held source. Only the returned terminal transaction can mint the one-use admission. |
| `SelectedCommitReference` / `CompactionOperation.require_summary_link` | Match the original operation/session identity and verify the digest of **original `SelectedSummaryAttempt.source_json`**, which is a separate unchanged request envelope. Never hash a reprojected `CompactionSource`. |
| `SelectedSummaries.link_commit`, `CompactionOutcomeSnapshot.read`, `OperationState.represents_summary` | Verify the same original link and committed native outcome. Outcome display does not grant input admission. |
| `SelectedSummaries.retire_unchanged` | Find original commits by their selected-summary operation ID; a matching native commit prevents no-write retirement. |
| `SelectedAdmissionIdentity` / `SelectedSummaryAdmission` | Consume the current typed request source and original revision/input proof, followed by the process-local returned ACK. No audit JSON reconstruction. |
| `tools/cutover/native_schema_carry.py` | Authenticate original selected request envelopes using the original producer, preserve their exact `source_json` bytes, then verify references from untouched intents against those bytes. Its request-envelope round-trip comparison is not a `CompactionSource` projection comparison. |

Repository searches for `intent_json`, `NativeIntent.read`, `SelectedCommitReference`, `source_json` verification and `FieldCodec.decode(CompactionSource, ...)` close the Python product and outside-src cutover consumers. The latter has zero callers. AST enumeration covers all 311 product modules with no parse omissions; it supplies static reference evidence, not a claim about dynamic Python resolution or external tools. Native receives the separately encoded `NativeRequest`, not the journal source projection.

The installed529 reader was also run read-only against the actual current journal. All **seven** original intents still contain `source.bus_revision`. All seven native request projections, committed outcomes, original summary links and original `source_json` digests verified. The journal file SHA stayed identical. No admission was minted and no original audit JSON was re-encoded. Evidence: `evidence/compaction-source-successor-20261002/old-intent-readers.json`.

## Actual configured gate

The distinct private fork used the original 42,924,939-byte saved state, configured Sol/HIGH settings and native5184, through the installed SDK/ACP/native path with automatic extensions. A real peer reply was published while its summary streamed. The provider then exceeded the plan's output budget: retained 5,534, reasoning 0, budget 4,096. The native guard refused it before commit; the original follow-up prompt was never issued. Source bytes stayed identical and children closed. The failed attempt remains preserved without replay at `/home/ts/wt/s52901`.

This is a **failed final gate**, not acceptance of commit/admission or a physical UI test. The independent summary-budget owner was notified. The source closure and original-record read verification remain reviewable; readiness requires an actually completed configured path, without replaying this uncertain provider attempt or weakening the budget guard.
