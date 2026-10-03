# S4 complete original model-step measurements

PR560 is merged. This follow-up reuses the same checkout and completed original
three-cut run; no provider calls, environment, package, worktree or source copy.

The source review found RecordedNativeProbe.read exports only the final assistant
usage in answer_usage. Its original corroborated branch already contains every
assistant tool/answer step. The cut2 and cut3 bash-assisted probes each have more
than one model response; exporting final usage alone omits earlier reported cost.

Extend RecordedNativeProbe to export each original assistant step and its PiUsage
through NativeEntry/MessageEntry's existing assistant_message contract. Remove
the final-only answer_usage projection across measurement consumers. Keep missing
counters unavailable, never zero; the original answer retains its own usage.
This measures journaled assistant completions, not unobserved provider retries or
billed spend. Question/RecallRound/ScoreView continue to own answer scoring.

Existing source/AST evidence: probe-consumers-after.json and continuation-source.json
in evidence/three-cut-configured-retention-20261003. Source inspection found one
answer_usage producer, no production/scorer readers, and the two original recorded
run entrypoints delegate to the same RecordedNativeProbe.read. No native lifecycle,
context, diagnostic or SQL changes. Final checks will consume completed originals
and exercise the existing scorer entrypoint without a model call.

Remaining source work: recorded matched-run alignment/construction eligibility,
separate unassisted-recall and tool-assisted task-quality comparison denominators,
and original constraint-backed action validity. Current ACTION exact answers are
not proof of an executed valid action. Missing SDK strings for original cuts1/2
remain unavailable. No expensive study is a blocker to these source changes.

## Working checkpoint

RecordedNativeProbe.model_steps now visits the same corroborated original branch
through NativeEntry.assistant_message, exporting original entry identity, timestamp
and PiUsage for every completion. NativeEntry/MessageEntry and PiUsage retain their
original meanings; no new model-step type, usage account or provider parser.
The final-only answer_usage field is deleted, with no compatibility alias. All
scorer entrypoints and configured driver inherit the new projection through read.

The existing installed scorer consumed the same completed three-cut originals,
without a provider call or native launch. It reports five model steps (1,2,2),
including both bash tool-call steps omitted by the earlier final-only projection;
answers remain9/9, stale0, missing0. All original560 artifact hashes still match.
One final batch's three existing controls passed; the new control initially
expected a null for an unavailable optional PiUsage field, while the original
codec omits it. Only that assertion was corrected and rerun, PASS. No source
behavior was weakened, and no broader tests or fixture runs were repeated.

`source.json` records the reused AST tool, original member behavior and all
measurement consumers. `original-completions.json` exports usage only; private
model content remains in the original protected root. This is fuller measurement
plumbing, not a matched-model result or cost-saving claim. The listed alignment,
quality-denominator and action-validity source work remains independent of any
paid-study approval. No runtime policy is activated.
