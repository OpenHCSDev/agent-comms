# Commander board and derived commit cadence

Issue #1495. Core #1937, paired Toad #1080. Owner: pi-cn-phase6.
These are source-paired changes, not an installation claim.

## Working slice

`/commander <commander-thread>` opens the ordinary Toad page. It shows held
orders, deadlines and delivery state, overdue first. Expanding one unit reads
its actual checkout commit and three original recent turns. Recorded failures,
stalled orders and retained refused sends appear in the alarms pane.

The collapsed card also shows commit cadence, the forecast next-commit time,
its margin against the order deadline, and whether that forecast has already
been missed. Missing checkouts and insufficient/non-increasing samples remain
explicitly unknown. Delivery removes the forecast.

**Interpretation:** commit cadence predicts the next commit, not completion.
There is no known amount of remaining work from which to calculate a completion
ETA. The page explicitly says `completion unknown`; no prediction is persisted.
Full row-27 prediction/outcome comparison remains unverified. pi-plan-claims
owns that row's declaration in #1992; no second parity registry was added here.
Strikes, review queue, story and sidebar decorations remain outside this slice.

## Owners and deletions

- `TodoStore.held_for_commander`, nominal holder identities and `Todo.overdue`
  supply the canonical order selection and deadline meaning.
- `ProblemOrder.instructions` supplies order text. `CheckedProblemOrder.ordered_at`
  owns recorded-time normalization; all numeric order-time consumers use it.
- `ProgressSource.progress` supplies one progress-selection algorithm.
  `StatusProgressSource` inherits it and disables acceptance-check execution.
- Main's `ProgressCheckout` family owns available versus removed checkouts.
  Its concrete classes supply commit details and bounded cadence evidence.
  Both distance and cadence reads share `PresentCheckout.branch_commits` and
  the `ProgressPoint` decoder. #1935's competing source changes were reverted.
- `UnitCard.momentum` derives `CommitMomentum` values from canonical orders and
  observed commits. Unknown and forecast states are distinct types, not zero
  timestamps standing in for missing observations. No new store or timer exists.
- `DeliveryProgress.failed_notifications` owns failure selection. `FailedProgress`
  returns the original notification. The alarm reader's `isinstance` filter was
  deleted; Toad has no failure-kind switch or notification copy.
- Actual GitHub command failures and timeouts become explicit unread progress.
  Other Git, decode and source errors propagate; there is no ValueError catch-all.
- Existing observation interests, store/WAL revisions and expiry notifications
  drive updates. Closing the page retires its interest.
- Toad renders the Core model through its existing Collapsible widgets. GoalBar
  presence is derived from actual composition, not an optional cached child;
  opening/clicking a bus conversation no longer raises `goal_bar` AttributeError.

Used the existing refactor-audit Package parser across both production roots.
The declaration/consumer evidence reports zero parse omissions. Dynamic family
and MRO resolution was read semantically, not claimed as an AST behavioral proof.
Evidence is under `/home/ts/.cache/agent-scratch/pi-cn-phase6/`.

## Physical application proof

`physical-failure-owner/` contains the latest plain-st/Xvfb run, using normal
Toad, its real observation process, copied registered saved sessions, real
scratch-bus orders created through Core tools, and an actual retained refused
send. No provider was started, and no original session or uncertain input was
changed. `sources.txt` records provenance.

The journey opened a real bus conversation, submitted `/commander claude-code`,
clicked one card, observed a tool-recorded delivery update without reopening,
and returned to the same conversation. Captures were inspected; no traceback
pane or unavailable-page error is present.

- `level-0.txt/png`: collapsed cards, known and unknown cadence, alarms.
- `level-1.txt/png`: one expanded card and original turns; second card collapsed.
- `live-delivery.txt/png`: delivery updates the same open card and clears its stall.
- `result.txt`: successful continuous-journey result.

Actual collapsed screen excerpt:

```text
Commander claude-code | 2 units | Overdue first
P1: 3.0m/commit; next commit ~17:17 (132.1m before due); forecast missed by 133.2m; due 1.2m ago; completion unknown
P2: cadence unknown (need two increasing commit times); due in 29.8m; completion unknown
STALLED pi-cn-phase6 P1: pushed d07ee61b8 at 17:14, no PR
REFUSED SEND #1 claude-code -> unregistered-page-target: Target 'unregistered-page-target' is not a registered thread.
```

## Checks and review limits

The stable source checkpoint passed **142 tests in 72.67s**. After removing the
failure-family filter, all **10 commander-model checks passed in 1.88s**, including
selection of the original failed notification. Tests ran in the background under
`timeout 300 ... -X faulthandler`, with default parallelism/coverage disabled.
The environment warns about the unavailable pytest-timeout plugin; the outer
timeout was enforced. Ruff for the new model and both diff checks pass.

The retained full ratchet reports at the stable checkpoint both exit **1**, solely
for `SourceLines` (+465 Core, +205 tools), with no introduced findings. Rechecking
`ca9c4f881` after deleting the failure-family filter gives **src 1/tools 1**, solely
+474/+205 SourceLines, again with introduced findings empty. No budget,
suppression or checker change was made. Under the commander's ruling in message
3617, SourceLines-only failures do not block review. Reports name their exact
base/head; they are not a claim about a fresh merger integration head.

The checking interpreter is the shared QA Python, with the paired Toad source and
existing stack dependencies visible. Toad now publishes its inline declarations
through `py.typed`; no copied Toad schema or stub was introduced. The installed
stack interpreter lacks the checking modules and was not modified.

The required `order pr P243` create command was attempted once. It failed on its
derived missing `pi-cn-phase6--P243` directory; this task's assigned checkout is
`pi-cn-phase6--P1`, and #1937 already exists. The existing PR is updated in place
with metadata generated by its canonical order owner. GitHub currently reports
a conflict with current main; no merge-main or unrequested rebase was performed.
