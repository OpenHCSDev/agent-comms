# Commander record plane

Owner: pi-cn-phase3. Branch: feat/cn-phase3-record.
Baseline: 5cdac561d498ece25142a8d2e5cf9b06002b6c8a.

## Checklist

- [x] Read the plan and inspect both repositories' open PR file lists.
- [x] Confirm existing verdict, strike limit, fire and void owners on main.
- [x] Add delivery integration to the canonical checked delivery.
- [x] Decode PR query rows into named records, not positional tuples.
- [x] Confirm real scratch-bus order, verdict, strikes, explicit fire and void through Core tools.
- [x] Confirm main/unmerged delivery and unlinked refusal through the CLI.
- [x] Pass the two bounded record/codec sanity checks. Pytest is unavailable.
- [x] Publish draft PR #1935; do not merge or install.
- [ ] Automatic firing at the strike limit, with all strike sources in the same write.
- [ ] Retain original typed transitions and the state each operation replaces.
- [ ] Connect commander board/done rendering after the recent shared-function overlap is settled.
- [ ] Verify a real order merged to a non-main target (codec checked, not live-proven).

## Source relationships

Before: `OrderDelivery.verified` ran the check and deletion counts; `CommitDelivery.require_source`
proved ancestry and publication only. Completion returned no integration fact. `PullRequests.read`
decoded a positional tuple of seven unrelated fields. `DeliveredProblemOrder.line` was a constant.

After: completion passes its canonical Todo and registry to the delivery owner. A checked commit
records a `DeliveryMerge`: main, another PR target, or linked but unmerged source branches.
`DeliveredProblemOrder.line` derives its text from that delivery. `WorkBranches` still owns the
open-PR/open-issue branch-link rule. `PullRequests.listed` now owns exact order-reference matching;
`ProgressSource` no longer repeats that identity decision. PR lookup still uses the immutable order reference, now decoded
through named query records including the target branch. No verdict, strike, delivery or branch store
was added. The integration is the observation at completion, not a later claim of installation.

Before/after AST evidence used refactor-audit's `Package.load` with Python 3.14: 382 Core production
modules parsed, zero omissions. The final installed-Toad syntax pass parsed 283 modules, zero omissions. The only production `OrderDelivery.verified` caller is `DeliverTodo.prepare`;
creation is in `commander.order.done` and `CommsOrderDoneTool`. Goal summaries consume the order's
`line`. The installed Toad source has no order-delivery consumer; its unrelated input-delivery types
are not this family. A preliminary NRA run under its old Python 3.12 omitted Python 3.14 syntax;
it is not acceptance evidence. NRA under Python 3.14 is blocked by that checkout's incompatible
metaclass-registry/tree-sitter dependencies; the existing refactor-audit parser supplied the census.
Dynamic tool invocation is generated from ToolRequest declarations/FieldCodec, not statically
resolved by this census.

## Overlaps

P603 is merged (#1757). Open #1903 and #1873 do not edit these record owners. Recent #1756 edits
FairText, not the delivery family; #1925 removes DeadlineText. Recent #1878 changes TodoStore's
owner lookup, which this change leaves alone. Older #1737 changes claims and closing, not integration;
older #1733 adds printed check output and must compose that output with the checked delivery here.
#1912 moves the commander functions into an explicit Commander context, including `board` and `done`.
Those functions are left untouched pending the integration owner's conflict decision.

## Resource boundary

`tools/agent_resource_check.py --assert-headroom` warned: about 4 GiB available RAM and 3.8 GiB
free on /tmp. No parallel agents or large suite. The prescribed /home/ts/bin launcher is absent.
Owned verification state is `/home/ts/wt/pi-cn-phase3--P1/.proof-record-plane` (real scratch bus,
copy of this unit's native transcript and temporary check trees). Logs are the checkout's `.proof-*.log`;
`.proof-test-state` is the empty intended pytest scratch directory. The check commands set TMPDIR
under the owned persistent root. These paths remain for the unfinished review; remove them after
acceptance, preserving the concise receipt below. The proof does not own other agents' /tmp worktrees.

## Actual CLI proof

Original response bodies: `docs/evidence/cn-phase3-record-results.jsonl` (12 actual responses,
normalized only for whitespace). Driver: `tools/proofs/cn_phase3_record.py`. The driver initializes
real Core stores and calls `python -m agent_comms.cli tool --from ...`. It uses no fake Comms,
fixture events or model replies. Judgment names a real edit from this unit's native session,
read through `OrderEditsRead` after copying the session to the scratch root. No original session
was changed and no live unit received these deliberate scratch-only strikes.

| Actual operation | P1 revision | verdicts | retained strikes | voids | assignment |
|---|---:|---:|---:|---:|---|
| create | 1 | 0 | 0 | 0 | record-worker |
| accepted verdict | 2 | 1 | 0 | 0 | record-worker |
| first strike | 3 | 1 | 1 | 0 | record-worker |
| second strike | 4 | 1 | 2 | 0 | record-worker |
| explicit comms_fire | 5 | 1 | 2 | 0 | null |
| void second strike | 6 | 1 | 2 | 1 | null |

After voiding, Core's `live_strikes(record-worker)` returns one. A void does not restore a released
assignment. The tool's assignee-only `strike_count` is zero once there is no assignment; the original
strikes and void are still retained on the Todo.

P2's real completion returned `todo.order.delivery.merge = {"kind":"main"}`. P3's real completion
on this published draft branch returned `{"kind":"unmerged","branches":["feat/cn-phase3-record"]}`.
P4 used an existing published unlinked commit, without editing or publishing that branch. Core refused:

```
Delivered commit 2874a87ada914add901c7f318ea46c3c88aa555a is not merged and has no branch linked to an open PR or issue.
```

P4 remained pending at revision 1. The check can still leave its normal Git check note even when
completion is refused. These are source-CLI checks of the candidate, not an installed runtime claim.
Nothing was merged, published as a runtime, restarted or installed.

## Local checks and precise blockers

- The requested background `timeout 300 python -X faulthandler -m pytest ...` failed before
  collection: the shared interpreter has no pytest. An isolated-venv attempt was refused by the
  existing StorageRule; no alternate installation was attempted. A shared test interpreter with
  pytest, or a reviewed dev-dependency installation by its owner, is needed for the suite.
- Final checks on source checkpoint ec5f94568: Core/Toad AST passes above, the candidate CLI entrypoint,
  and both standalone record/codec sanity functions passed under the same bounded/background
  command form. They check every DeliveryMerge member and named external non-main target decoding;
  they are not substituted for the actual CLI proof.
- The real second-strike response still held P1. Main has a limit predicate and an explicit fire tool,
  not automatic firing. The shared TodoStore/Goals write-path work overlaps recent #1878 and is raised
  with claude-code. Source inspection also finds destructive order/assignment replacement, missing Core
  recording timestamps and direct fold/correction writes. pi-cn-phase3 retains this implementation.
- pi-cn-phase5 owns SendBackVerdict/Goals.send_back publication, OrderPost rows/insertion and the
  canonical order channel. Those changes must remain in the same transaction as retained operations;
  this checkpoint does not edit their classes or TodoStore.rows/apply. Their exact published commit
  is a dependency for that continuing write-path change.
