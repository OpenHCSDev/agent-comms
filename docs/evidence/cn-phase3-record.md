# Commander record plane

Owner: pi-cn-phase3. Branch: feat/cn-phase3-record.
Baseline: 5cdac561d498ece25142a8d2e5cf9b06002b6c8a.

## Checklist

- [x] Read the plan and inspect both repositories' open PR file lists.
- [x] Confirm existing verdict, strike limit, fire and void owners on main.
- [x] Add delivery integration to the canonical checked delivery.
- [x] Decode PR query rows into named records, not positional tuples.
- [ ] Confirm real scratch-bus order, verdict, strikes, fire and void through Core tools.
- [ ] Confirm delivery through the CLI and run bounded local checks.
- [ ] Publish a reviewed draft PR; do not merge or install.

## Source relationships

Before: `OrderDelivery.verified` ran the check and deletion counts; `CommitDelivery.require_source`
proved ancestry and publication only. Completion returned no integration fact. `PullRequests.read`
decoded a positional tuple of seven unrelated fields. `DeliveredProblemOrder.line` was a constant.

After: completion passes its canonical Todo and registry to the delivery owner. A checked commit
records a `DeliveryMerge`: main, another PR target, or linked but unmerged source branches.
`DeliveredProblemOrder.line` derives its text from that delivery. `WorkBranches` still owns the
open-PR/open-issue branch-link rule. PR lookup still uses the immutable order reference, now decoded
through named query records including the target branch. No verdict, strike, delivery or branch store
was added. The integration is the observation at completion, not a later claim of installation.

AST evidence used refactor-audit's `Package.load` with Python 3.14: 382 production modules parsed,
zero omissions. The only production `OrderDelivery.verified` caller is `DeliverTodo.prepare`;
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
Owned verification state and logs will remain under this checkout's evidence directory, not /tmp.
