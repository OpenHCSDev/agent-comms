# S4: Repeated-compaction retention evaluation

**Head audited:** `697bba42f5f03e169ff8eae9490090cbd0d0b89e`.
**Rules:** [00-RULES.md](00-RULES.md). **Step 1 scaffold, step 4 native/model runs.**
**Shared abstractions** ([02-SHARED-ABSTRACTIONS.md](02-SHARED-ABSTRACTIONS.md)). *Builds:* none. *Uses:* existing retained native fixture and lifecycle journeys.

## Gap and supplied infrastructure

**Lifecycle/continuation tests do not answer whether a model retained relevant facts.**
This PR supplies `tests/compaction_retention_fixture.py` and its provider-free
tests. One authored synthetic scenario has three cumulative history snapshots
and seven exact-answer questions per round: symbol, file, export root, goal,
source revision, unresolved failure and input disposition. Later rounds supersede
corrections, rename source, replace goal and change commit. They deliberately keep
failure/input unresolved. This is a seed fixture, not representative coverage.

Question owns its expected answer and stale answers; RecallRound computes answer
outcomes. ScoredRound/ScoredScenario derive counts through the shared ScoreView
contract, without stored totals or copied source identities. Condition labels are
value-only experiment names,
not production behavior dispatch. New question/scenario does not require an edit
to the scoring algorithm (MEMB-1/IMPL-5 avoidance). Truth comes from authored
fixture events, not candidate summaries (IDEN-1).

Exporter omits expected/obsolete/evidence metadata from held-out questions. Their
answers necessarily occur in the source history, as a recall test requires.
Answers JSON maps round ID to question ID to exact answer string. Missing answers
are counted, not dropped; unknown keys and duplicate/malformed payloads reject.
Exact matching is intentionally strict for these identifiers, not a general
semantic grader. No regex matches, substring credit or LLM judge is claimed.

## Run now

```sh
python -m unittest discover -s tests -p test_compaction_retention_fixture.py -v
python tests/compaction_retention_fixture.py
python tests/compaction_retention_fixture.py --condition task-memory --answers /persistent/path/answers.json
```

All four labels use the same oracle: full-context, bounded, task-memory and
recent-only. Labels do not construct those context conditions. The scorer accepts
externally recorded responses; no model/compaction adapter exists in this PR.
Fabricated self-answers can test the scorer, never produce a retention result.

## Required next infrastructure and owner boundaries

Reuse `tests/retained_native_fixture.py`,
`test_selected_owner_compaction_integration.py`,
`test_retained_manual_compaction.py` and `test_selected_execution_native.py` for
actual saved-native/ACP/provider journeys. Do not duplicate native launch,
credential, commit or goal fixtures. S4 measures them; it never owns task state or
production context selection. Trace their actual source/guard scope at dispatch.

An evaluation runner must record immutable implementation/model/settings,
scenario seed, round, actual context construction and digest, summary/checkpoint
IDs, probe/map/synthesis request counts, timing, full usage/cost, and raw answer
artifact reference. Independently score canonical fact availability, prompt
presence and model recall. Freeze questions before runs; do not put the oracle in
provider prompts or repair a history after seeing candidate results. Full-context
control runs only where the exact same history fits; explicitly mark ineligible
runs rather than truncate that control silently (BOUND-2).

Add coding, research and long-running-goal traces. Include corrections crossing
summary segments and repeated split turns, Unicode/exact paths, and missing
source evidence. At least three sequential real checkpoints per applicable trace.
Randomize/repeat controls with same model and report sample counts/distributions.
Predeclare quality/cost/latency margins. Require zero stale-authority or UNKNOWN
mutation in exact-state controls; numerical recall margins need agreement before
model results. External paid run authorization is a separate gate.

## Revision mass and lock-in acceptance gates

Add the four-class [S2 retention contract](S2-MEMORY.md) to the evaluation design.
Retrieval accuracy alone does not test whether a summary discarded alternatives
or enlarged the valid-action set. The existing fixture/scorer does not implement
these additional metrics or probes.

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

This operational metric is motivated by the user's `rec_P`/Paper4b proposal;
formal equivalence to that notation remains unverified. Constraint drift target is
zero, and zero unauthorized Decision revisions is the proposed exact-state gate.
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
model probes still require authorization. No additional native/model adapter or
metric implementation is claimed by this planning update.

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
respecting enforced merge rules and reporting unexecuted CI accurately. Current provider-free
checks verify only the oracle/scorer and exporter. No full-suite, native acceptance,
model-quality or deployment claim is implied.
