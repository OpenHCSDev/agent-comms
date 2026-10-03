# S4 matched original request and quality evidence

Continuation of merged573 in the same checkout. No runtime policy, native
artifact, provider input or comparative study is proposed.

The existing RecordedNativeProbe owns manifest/request binding and supplies
typed RequestProgress records. RecordedNativeProbes owns matched-original
alignment. ScoreView/ScoredScenario own question denominators. Extend those
owners; do not create a condition registry, model resolver or second scorer.

The current comparison checks fork source, configured selection and tools, but
does not consume original request/model/budget evidence. Paired quality then
classifies answers without receiving its alignment result, so an unavailable
match can still produce an evaluated difference. Close both consumers together:
derive selected-model/capacity observations from the original admitted request,
retain absent allowances as unavailable, and keep unmatched questions visible
without treating them as a paired result. Condition labels still cannot certify
an intervention or complete-history eligibility.

Source/AST owner and consumer review precedes implementation. One final bounded
measurement check and the existing completed-record scorer confirm the change;
there is no new configured run, three-cut rerun, build or paid comparison.

## Published source and final check

RecordedNativeProbes now consumes the probe's exact typed request observations.
PiModel.require_selection corroborates the captured configured selection; the
scorer does not parse a second model identity. Original context/output capacity,
initial output intent and minimum output are compared only when observed. Revised
allowances remain ordered; different input estimates/admitted allowances are not
silently treated as an intervention mismatch. One same_observations method now
serves both request and completion fields, deleting the copied comparison logic.

RecallScenario.compare_native passes its original alignment to paired_quality.
ScoredScenario retains unmatched questions in a separate denominator and emits
no difference for them, for missing observations or for an empty group. The
CLI/native/comparison and control callers use that same relation; there is no
scalar compatibility call. Individual original-answer scoring is unchanged.

19 scorer/control lines deleted, 140 added; runtime/native source delta is zero.
Existing NRA Package parsing finds the same nine relevant class declarations
before and after, with zero omissions. The 36 original lexical call sites and 51
final sites include the new shared method and its controls. Dynamic JSON readers
and MRO execution are outside that lexical census. No class/scanner was added.
[Before](../../evidence/recorded-matched-request-quality-20261003/source-before.json)
and [after](../../evidence/recorded-matched-request-quality-20261003/source-after.json).

The final batch passes 26 controls in 1.734s. They prevent unavailable request
facts and unmatched answers becoming paired scores, and detect model/capacity/
intent mismatches while preserving revised allowances and existing denominators.
The installed Core573 decoder plus current scorer consumes the original three-cut
records: 3/3 unassisted, 6/6 assisted, five model completions unchanged. The actual
comparison CLI refuses reuse of the same run as its own control; its negative
exit/log is preserved. Both prior Started inputs remain identical. No build,
environment, native launch, provider input, replay or public mutation occurred.

[Recorded scorer receipt](../../evidence/recorded-matched-request-quality-20261003/recorded-scorer.json).
The positive pair path is qualified by composed-record controls, not a genuine
matched provider dataset. All three historical cuts still lack request metadata.
Actual intervention construction, full-history capacity, HTTP bytes, returned
model/effort and the separate thirty-pair study remain unqualified. This change
does not activate a runtime policy or certify those missing facts from labels.
