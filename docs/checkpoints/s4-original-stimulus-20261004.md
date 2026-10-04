# S4 original stimulus binding and recorded workflow usage

Ready implementation: `044a4804`, on merged630 main `25055aa6`.
PR631 changes existing evaluation fixtures/consumers only; `src/`, `stack/`
and `tools/` are unchanged. No new class, codec, lifecycle or evidence store.

## What changed

`RecordedNativeProbes.stimuli` references completed source inputs using the
existing `RecordedNativeProbe`, separately from recall responses. The original
input proof, native user/terminal and optional STARTED InputDocument continue
to own input corroboration. `NativeContextRecord.input_commit` supplies the
original typed native input coordinates without another identity declaration.

`RecallScenario.construction_rounds()` supplies exact new-history `source_text`
from its existing prefix calculation. Observation receives that same scenario;
it no longer takes only rounds and reconstructs an oracle inside the reader.
`RecordedNativeProbe.source_delivery()` checks exact authored text and requires
both the user and successful terminal before the selected original boundary.
Same-session membership uses `NativeEvidenceRead.branch`; cross-session probe
membership additionally uses `NativeForkCreation.covered_prefix`, with its
original file identity, prefix bytes and parent-source checks. Missing or
unrelated inheritance refuses. A cut with no declared SDK parent relationship
cannot qualify a cross-session stimulus. This is pre-boundary presence, not a
claim that compaction replaced it or the provider received complete history.

`RecordedNativeProbes.inputs` groups stimuli and recalls through the same
bounded `original_readers` acquisition. Unknown rounds and duplicate original
inputs refuse; all resources close on refusal. Original continuation membership
now includes declared stimuli too, with its existing STARTED/source/terminal
fences. Scoring, comparison, CLI and configured continuation callers receive
the same three acquired groups; the old two-group consumer path is deleted.
Shared preparation is allowed between paired arms, while the existing distinct
recall-session/input requirement remains. This does not establish independence.

`ScoredScenario.recorded_resources()` adds `source_inputs` and
`recorded_workflow`. The existing summary/recall `combined` total keeps its
scope. Every source model step uses its original PiUsage; missing counters or
rounds remain unavailable, and real zero remains zero. Shared source work is
not independent arm cost, and summing per-arm workflow totals would double-count
shared preparation. These are recorded completions, not billed spend or a full
retry/HTTP/timing account. The related tool-catalog alignment consumer now uses
its declared class, deleting a stale comparison against the former string name.

Historical stimulus absence remains explicit in `source_delivery`, rather than
retroactively filling old inputs. The original functional three-cut case remains
its accepted scope; it is not converted into an authored matched experiment.

## Source and final validation

Existing refactor-audit `Package` parsed src311/tests365/tools53 before and after
with zero omissions. Source/caller evidence remains in
`.artifacts/s4-original-stimulus631/owner-consumers.json`. Required declarations
are the existing scenario construction, run acquisition, native probe binding
and scored-resource owners. Migrated consumers include `score_observed`,
`score_native`, `compare_native`, continuation and all direct resource controls.
No production/tool caller uses these measurement methods. This is lexical AST
coverage, not a proof of dynamic execution.

Final focused batch: **15 passed, 15 deselected, 6 subtests, 0.27s**:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:tests python -m pytest -o addopts='' -q tests/test_recorded_retention_measurements.py -k 'stimulus or workflow or group or construction or resource or alignment'
```

Controls detect edited/future/sibling source credit, missing or corrupt SDK
inheritance, omitted source readers and leaked resources, duplicate/unknown
inputs, scenario reconstruction, omitted model work, unavailable counters
becoming zero, and changed summary/recall totals. Authored journal controls
exercise the existing byte/branch owners; plumbing spies do not supply actual
native input, SDK fork creation or model-recall evidence.

The actual scorer CLI exported changed construction text, kept all three
missing stimulus rounds and workflow totals unavailable, and refused an unknown
stimulus before file acquisition. The original nine630 design/oracle/plan files
are byte-unchanged; new outputs do not replace their historical hashes. Ten
prospective pairs are **unexecuted**. See the
[CLI receipt](../../evidence/s4-original-stimulus-20261004/cli-receipt.json).

Raw first failures are retained in `.artifacts/s4-original-stimulus631/`.
An attempted broader collection had no installed ACP dependency and admitted no
native/provider work. The first source batch exposed the stale catalog consumer
and malformed authored boundary records. The later remaining negative was a
control expecting ValueError where the original prefix owner correctly raises
NativePiUnavailable; the final control uses that existing refusal family.
No exception boundary or source check was relaxed.

No provider/model/SDK process, original native read, package/environment,
artifact loan or public mutation. W1/W5 remains Einstein's runtime/native claim.
This is source/scorer construction binding and recorded accounting, not executed
matched interventions, full-history capacity, preregistration, independent
sampling, recall margins, HTTP/billed accounting or end-to-end/S1 acceptance.
The separate30-pair/USD75 study remains unapproved and unstarted.
