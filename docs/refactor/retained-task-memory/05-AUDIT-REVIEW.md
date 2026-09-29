# PR428 ownership and audit review

Current owner: `comms428`. Dedicated worktree: `/home/ts/wt/comms428`.
Prior draft publication remains attributable to `openhcs-pr159-viewer-bind-owner`.
User requires refactor-audit and nra-refactoring tools before code edits and no
state duplication ever. Every consumer must derive from its authoritative source.

## Checklist

- [x] Create independent worktree from remote PR428 head `93c2f329`.
- [x] Normally merge remote main `7995bc5a` into this worktree (`452bc01f`).
- [x] Read project prompt, owner decisions and current NRA skills.
- [x] Inspect PR feedback: no reviews/comments, CI queued at `93c2f329`.
- [x] Complete scoped contextual NRA scan and inspect coverage/raw leads.
- [x] Run skill census/overlay and verify findings against source.
- [x] Record refreshed source-owner receipts after merged main changes.
- [x] Rerun the eight provider-free scorer/exporter checks successfully.
- [ ] Replace the scorer's implicit aggregate record with an authoritative derived
      view, migrate all callers and rescan. Do not store duplicate score totals.
- [ ] Publish tested review/evidence update to the existing PR428 branch.

## Source and exact coverage

Audited source: `452bc01f977eb616016f78e87e9e2d937c960864`.
NRA source: `673c062fc656e9c74f1eddcab30f036c9befbc1f` in
`/home/ts/wt/nra-bounded-full-audit-20260929`. Python 3.14, one worker.
[Machine-readable receipt](evidence/scaffold-audit-20260929.json) records raw
findings, original class rows, coverage and skill measurements.

Report roots are the two scaffold Python files. Analysis roots are the complete
`src/agent_comms` package plus those same two files. Source index: 285 files.
The completed full-payload scan retains all raw findings and the source index.
The subsequent agent-payload cache receipt explicitly reports `complete: true`,
85 analyzed detectors, zero omitted detectors and exact validated cache identity.
This is complete detector coverage for the declared context with results filtered
to the two scaffold files, not a claim that production has zero findings.

Reproducible command (substitute the owned output path and interpreter):

```sh
PYTHONPATH=/home/ts/wt/nra-bounded-full-audit-20260929 python -m nominal_refactor_advisor \
  tests/compaction_retention_fixture.py tests/test_compaction_retention_fixture.py \
  --include-tests --context-root src/agent_comms \
  --context-root tests/compaction_retention_fixture.py \
  --context-root tests/test_compaction_retention_fixture.py \
  --no-auto-context-root --parse-workers 1 --analysis-workers 1 \
  --scan-budget-seconds 150 --json --json-payload full --raw-findings
```

The full payload does not emit scan_status on this path. Use the same command
with `--json-payload agent` and the same owned cache directory to obtain the
explicit coverage receipt; do not treat absent metadata as a completeness proof.

## Failed attempts and tool custody

The first contextual command specified only `src/agent_comms` as an explicit
context root. NRA raised CacheCheckoutPathError for a selected report file inside
the same checkout. `nra-architecture` received the source/command/error receipt
for tool ownership. Declaring the selected files as context roots admitted them.
No tool implementation was patched here.

The next command hit the tool's default 20-second deadline at
`contextual_global_prepare:closed_parameter_conveyor`, with `complete: false` and
exit 124. Explicitly setting 150 seconds completed the scan. The full output
reported 11.335 seconds on the later partially warmed analysis-cache run. The
coverage cache hit took 1.057 seconds. These are scoped observations, not an
NRA-wide performance guarantee.

The skill CLIs accept directory roots and append a slash to the git pathspec.
Passing a single filename therefore measured zero files. Those zero outputs
were rejected, not reported as clean. Reuse the skill's `Repository.measure`,
`Census.of`, `Package`, `ParsedModule` and `Overlay.run` APIs for the two selected
files. Feed NRA's existing parsed modules into the overlay, with no new parser
or detector implementation. Both files parsed; 307 code lines were measured.

## Class-first census and source adjudication

All six original scaffold classes joined uniquely to NRA's class-family index:
Condition, AnswerScore, Question, RecallRound, RecallScenario and
RecallMeasurementTests. Original lines, ordinals and bases are in the receipt.
No conditional/nested class was silently admitted or discarded.

NRA returned four raw findings: two unmodeled aggregate record shapes and two
semantic-mirror candidates naming AnswerScore. The overlay corroborates three
raw-shape sites. The skill census counts 30 string-keyed subscripts; no string
or type dispatch and no long boolean chain. Syntax counts are leads, not quality
scores or permission to rewrite unrelated production mechanisms.

### Bounded required-answer relation

The authorized scorer contract requires exact/stale/missing classification per
question, then counts of those outcomes per round and scenario with a fixed
question denominator. Those are different facts:

- `Question.score` determines a single `AnswerScore` of boolean flags.
- A round total is a cardinality over that round's actual AnswerScore collection.
- A scenario total is a cardinality over the scored rounds, not another truth
  table or independently writable total.
- CLI JSON is a derived boundary representation, not scoring authority.

Counterevidence to the tool's candidate: the raw aggregate `correct`/`stale`
fields are integer counts, not AnswerScore's boolean flags. Identical spellings
and a detector certificate do not justify using AnswerScore as their owner.
The unmodeled aggregate-schema lead is supported by source: RecallRound returns
an anonymous record; RecallScenario and tests repeat its literal keys. Candidate
repair is a typed scored-round/scenario view deriving counts from those actual
outcomes. No stored counts, freshness flags, metric registry or parallel cache.

Required pairs: Question -> per-answer classification; owned scored outcomes ->
aggregate counts; scored rounds -> scenario counts; scored result -> CLI export.
Forbidden pairs: raw CLI dictionaries -> internal scoring authority; caller-
supplied totals -> accepted score; an aggregate count -> a boolean answer fact.
This relation is admitted from the requested evaluation semantics and the existing
fixed-denominator tests, not from field-name similarity. A source/effect-preserving
migration and fresh scan remain required before reporting the repair complete.

Pattern risks: BOUND-2 (raw internal consumer bypass), IDEN-1 (boolean/count
conflation) and MEMB-5 (anonymous record shape repeated at consumers). Do not
fabricate an executable NRA recipe or equivalence proof for an authored new type.
New-case experiment: add a question without changing the aggregation algorithm.

Overlay alternatives also remain explicit: coding_scenario's 114-line length is
fixture data, not proof of a missing lifecycle owner; subprocess.run is the actual
CLI behavior check; the test module is collected by unittest/pytest, so the
import-only dead-module lead does not justify deleting it.

## Refresh after main integration

Merged PR417 now owns typed SummaryData response behavior. In this source:
`owner_compaction_adaptive.py:132-153` still consumes the effective selected
threshold; `pi_summary_payloads.py:123-283` owns selected response/decline/summary/
failure behavior. Extend these owners rather than reintroducing the retired raw
response switch. Original receipts at `697bba42` remain historical source evidence,
not current line-number guarantees. Production ownership/transport effects and
native JavaScript proof remain separate from this scaffold review.

## Execution resources and scratch

Owner: `comms428`. Audit JSON and caches are under
`/home/ts/.cache/agent-scratch/comms428-audit-20260929`; preserve commands and
receipts before removing owned scratch after workers exit. The tool defect's
failed stderr remains until the receiving owner has the evidence it needs.
Headroom warning: 12.3 GiB RAM available, 19.6 GiB home disk free, 11.0 GiB swap
used. Runs use the existing interpreter and skill APIs, one worker, bounded
shell/tool deadlines, no extra agents or full test fleet. No live state or
provider calls are involved.
