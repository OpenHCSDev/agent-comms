# S4: Repeated-compaction retention evaluation

**Historical plan baseline:** `4295d680`. Current measurement continuation: PR557;
current S3 implementation: merged527, qualified functional02 on native960.
The older baseline is not a request to rediscover or reimplement that capability.
**Rules:** [00-RULES.md](00-RULES.md). **Step 1 scaffold, step 4 native/model runs.**
**Shared abstractions** ([02-SHARED-ABSTRACTIONS.md](02-SHARED-ABSTRACTIONS.md)). *Builds:* none. *Uses:* Decision, existing retained native fixture and lifecycle journeys.

## Gap and supplied infrastructure

**Lifecycle/continuation tests do not answer whether a model retained relevant facts.**
This PR supplies `tests/compaction_retention_fixture.py` and its provider-free
tests. One authored synthetic scenario has three cumulative history snapshots
and seven exact-answer questions per round: symbol, file, export root, goal,
source revision, unresolved failure and input disposition. Later rounds supersede
corrections, rename source, replace goal and change commit. They deliberately keep
failure/input unresolved.

The task-quality continuation adds frozen research and long-running-goal traces
in `tests/fixtures/retention/`. `--scenario-file` decodes either authored oracle
into the same `RecallScenario` used by exports and recorded-native scoring. Each
has three authored source cuts, with retained valid alternatives, a later goal
that revisits one, explicit corrections and continuing prohibitions. These are
source fixtures; they are not three completed native compaction checkpoints.

`Question.measurement` labels recall, prohibition, alternative and action-choice
answers. `ScoreView` derives separate totals with the same exact-answer algorithm.
A remembered identifier cannot hide a lost prohibition or invalid action choice
in an aggregate score. The action score measures the frozen approved answer,
not execution of an action against a runtime constraint. Runtime revision mass,
provider prompt presence and model-quality margins remain unqualified.

Question owns its expected answer and stale answers; RecallRound computes answer
outcomes. ScoredRound/ScoredScenario derive counts through the shared ScoreView
contract, without stored totals or copied source identities. Condition labels are
value-only experiment names. A new question/scenario uses the same scoring
algorithm (MEMB-1/IMPL-5). Truth comes from authored
fixture events, not candidate summaries (IDEN-1).

Exporter omits expected/obsolete/evidence metadata from held-out questions. Their
answers necessarily occur in the source history, as a recall test requires.
Answers JSON maps round ID to question ID to exact answer string. FieldCodec
decodes that boundary into RecordedAnswers; retain the JSON duplicate-key hook.
Internal scoring consumes the typed record. Missing answers keep the denominator;
unknown keys and malformed values reject. Use exact identifier matching rather
than regex, substring credit or an LLM judge.

## Run now

```sh
python -m unittest discover -s tests -p test_compaction_retention_fixture.py -v
python tests/compaction_retention_fixture.py
python tests/compaction_retention_fixture.py --condition task-memory --answers answers.json
python tests/compaction_retention_fixture.py --scenario-file tests/fixtures/retention/research.json --probe-prompts
python tests/compaction_retention_fixture.py --scenario-file tests/fixtures/retention/goal.json --native-probes original-probes.json
python tests/compaction_retention_fixture.py --recorded-run original-run.json
python tests/compaction_retention_fixture.py --construction-plan --comparison-design design.json --sampling-seed 20261004
```

All four labels use the same oracle: full-context, bounded, task-memory and
recent-only. For native/model evaluation, construct each condition in the runner
and record its context digest.
The scorer accepts recorded responses. Authored answers test the scorer; model
retention requires actual model responses.

`--construction-plan` uses the existing `PairedRecallDesign` and its pinned
oracle to export each round's new history and unchanged public probe, plus
prospective paired-arm order from an explicit sampling seed. It refuses source
snapshots that rewrite the preceding prefix. History text is emitted once,
rather than duplicated across every prospective sample and arm. Sampling and
inference seeds are independent. This source export launches no model, creates
no native cut/input and proves no intervention/capacity/registration/approval.
Only the history/probe operands belong in the configured execution fixture;
the complete plan includes private oracle metadata and is not a provider prompt.

`--recorded-run` uses the existing RecordedNativeProbes input with `rounds` and
unprobed `checkpoints` maps, plus optional original `stimuli` references. It validates distinct ancestor cuts in frozen round
order through one borrowed original native source. Per-probe checkpoint,
`sdk_context` and `context_manifest` references belong on RecordedNativeProbe.
Checkpoint `registry_scope` references an original RegistryDocument capture;
the document supplies its snapshot. Original certified `wire` corroborates task
publications for scoped revision measurements. Missing original evidence stays
unevaluated. SDK prompt presence is separate from final HTTP payload presence.
Original NativeSummaryPayload and every original assistant step's PiUsage supply available counters;
absent counters are never measured zeros. See
[the receiving scope](../../checkpoints/repeated-retention-runner-20261002.md).

RecordedNativeProbe exports `model_steps` from the same corroborated original
input-to-answer branch. This includes assistant tool-call steps before the final
answer. Each step keeps its original entry identity, timestamp and complete
PiUsage record; unavailable usage remains unavailable, and missing optional
counters retain the codec's omitted-field representation. The former final-only
`answer_usage` projection is removed. The original
answer record still retains its own usage. These are journaled completions, not
a transport-attempt/retry count, actual billed spend or provider-wait duration.

Recorded source inputs reuse `RecordedNativeProbe` in the run's `stimuli` map.
The existing scenario construction owner supplies exact new-history `source_text`;
the acquired original user/terminal must precede its cut or probe. An inherited
probe also needs the original SDK prefix relationship. All stimulus/probe sources
are borrowed in one reader group; missing original references remain unavailable.
`source_inputs` reports their model steps separately. `recorded_workflow` requires
those steps as well as the original summary/recall groups; `combined` retains the
summary/recall scope. Shared source preparation is not independent arm cost.
These measurements do not prove a condition installation, complete-history
capacity, preregistration or a matched study. See
[original stimulus binding](../../checkpoints/s4-original-stimulus-20261004.md).

The submitted bounded SDK transform is reported only when its existing original
converter/request binding is complete. Partial transform/source evidence remains
visible; it does not become a bound request. The scorer groups that observation,
source delivery and full-history eligibility against all frozen rounds, and the
paired result retains both arms' evidence. Supplied condition labels and SDK
previews still do not authenticate intended matched intervention construction.
See [condition evidence](../../checkpoints/s4-condition-evidence-20261004.md).

SDK construction now retains raw AgentMessages with the original SDK source
witness. All four conditions use one selected SessionContext installation path,
with current ContextBudget admission. Authored installed086 construction/restore
and source/mutation/budget refusals qualify that SDK operation only. Actual
submitted-condition selection still requires original converter/request binding;
no matched interventions or model recall follow from installation. See
[condition installation](../../checkpoints/s4-native-condition-installation-20261004.md).

## Required next infrastructure and owner boundaries

Reuse `tests/retained_native_fixture.py`,
`test_selected_owner_compaction_integration.py`,
`test_retained_manual_compaction.py` and `test_selected_execution_native.py` for
actual saved-native/ACP/provider journeys. Do not duplicate native launch,
credential, commit or goal fixtures. S4 measures them; it never owns task state or
production context selection. Trace their actual source/guard scope at dispatch.

Preserve S1's compaction-feedback and post-compaction-continuation journeys and
its per-journey p95 budgets in every timing comparison.

An evaluation runner must record immutable implementation/model/settings,
scenario seed, round, actual context construction and digest, summary/checkpoint
IDs, probe/map/synthesis request counts, timing, full usage/cost, and raw answer
artifact reference. Independently score canonical fact availability, prompt
presence and model recall. Freeze questions before runs; do not put the oracle in
provider prompts or repair a history after seeing candidate results. Full-context
control runs only where the exact same history fits; explicitly mark ineligible
runs rather than truncate that control silently (BOUND-2).

Collect actual native checkpoints for the supplied coding, research and long-running-goal traces. Include corrections crossing
summary segments and repeated split turns, Unicode/exact paths, and missing
source evidence. At least three sequential real checkpoints per applicable trace.
Randomize/repeat controls with same model and report sample counts/distributions.
Default quality gates: zero stale authority, zero UNKNOWN mutation, zero
unauthorized constraint/Decision revision mass, and no held-out invalid action.
For model recall use a paired 95% confidence interval: candidate minus bounded
control lower bound >= -2 percentage points. Cost and end-to-end p95 latency must
each improve by at least 10% without failing S1's local budgets. Register margins,
sample count and spend before runs; changes require approval before viewing model
results. Default paid-run budget is zero until explicitly authorized.

## Revision mass and lock-in acceptance gates

Add the four-class [S2 retention contract](S2-MEMORY.md) to the evaluation design.
Retrieval accuracy alone does not test whether a summary discarded alternatives
or enlarged the valid-action set. Extend the runner and fixtures with these
metrics and probes.

For each adjacent checkpoint pair, match constraints and Decisions by canonical
owner identity, scope and original source reference. Derive **unauthorized revision
mass** as the number whose effective value changed without an explicit authorized
correction event in the original wire evidence, divided by the number of identities
eligible for comparison. Report constraints and Decisions separately, numerator,
denominator and round; do not store another authoritative set of retained values.
An explicit correction must name the affected identity, have update authority and
precede the checkpoint. An unrelated message, inferred intent or a summary edit
is not a correction event. Authorized supersession is reported separately, not
counted as drift or used to hide it.

A disappeared required item counts as an unauthorized revision unless source
scope/lifetime ended through a sanctioned event; otherwise loss would disappear
from the denominator. Additions are reported separately and checked against their
source; fabricated additions cannot pass merely because they lack a prior identity.
If no identities are eligible, report not-applicable rather than a perfect zero.
If source/correction evidence is absent, mark the comparison unevaluable and fail
exact-retention acceptance rather than assuming no correction occurred.

Revision mass (`rec_P`) follows paper4b. The exact-state gate is zero unauthorized
constraint and Decision revisions.
An authorized revision must preserve the original evidence and alternatives while
projecting the new effective value. Forced artifact references can change with
their source owners and must not be mislabeled constraint/Decision drift.

Freeze **held-out distinction probes** before constructing summaries. Early goals
should not require some declared valid-but-rejected alternatives; a later goal
should require revisiting one of them. Score both the ability to name the retained
alternative and the validity of the resulting revised action against the original
constraints, with the same trace and authorized correction events for all controls.
Include a forced-fact control, a dropped-prohibition case, an unsanctioned Decision
rewrite, and a valid explicit correction. Keep oracle alternatives and acceptance
labels out of provider prompts. Retrieval, authority preservation and lock-in
resistance are separate reported outcomes, not one aggregate quality number.

Add provider-free controls proving that an unchanged item scores zero drift,
unauthorized change/deletion scores drift, authorized correction does not, unrelated
correction cannot excuse drift, and missing provenance fails closed. Then exercise
the actual saved-native/ACP projection path before making retention claims. Paid
model probes still require authorization.

## Negative tests, guards and done when

Existing native/provider failures, stop, source change, goal replacement, restart
and uncertain settlement must still preserve input/history. New scorer tests
prove stale and missing facts fail, unknown keys reject, and every condition uses
the same oracle. One new-case test adds a question without changing evaluation.
No internal-format golden or structural tests of removed runtime contracts.

Done requires a runnable native evaluation entrypoint, representative frozen
fixtures, authorized matched-model results and acceptance margins, reproducible
reports, and guards separating measurements from authority. Include the revision-mass
and held-out lock-in gates above, not only retrieval questions. CI queue latency
does not hold useful checkpoints: publish after focused local verification, while
respecting enforced merge rules. Provider-free checks verify the oracle/scorer and
exporter; native and model runs establish the corresponding acceptance results.

S4 owns the task-aware timing default flip. When the predeclared margins and S1's
journeys pass, turn the default on in the same implementation PR. If they do not
pass, post the measured result to Tristan's queue with the explicit decision:
flip anyway, change the design, or drop the feature. S4 closes only with the
default enabled or Tristan's recorded disposition; it cannot leave timing opt-in
without that decision.
