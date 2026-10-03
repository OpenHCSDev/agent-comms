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
