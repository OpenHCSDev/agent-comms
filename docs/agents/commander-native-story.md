# Commander-native phase 5

Owner: pi-cn-phase5. Branch: feat/cn-phase5-story. Base: origin/main 011084fc5.

## Delivery checklist

- [x] Native chronological order story from original retained records (coverage limits below).
- [x] Native newest-first fleet story from original strikes and send-backs.
- [x] Real scratch-bus lifecycle, using public tools and actual unit records.
- [ ] Remove script board/narrative writers and roast after shared-function ownership clears.
- [ ] Record-owned send-back channel publication, once.

## Source and ownership

The records already own order issue and delivery times (`CheckedProblemOrder.at`,
`OrderDelivery.at`), strikes, releases, voids and verdicts. Their rendering belongs
on these owners, not in a script or a switch over their kinds. The native results
are transient views, not another store. Fleet membership belongs to the event
capability; it is not a roster of kind names. Work comes from the existing
`OrderWork`/`OrderCall` reader, never bus scanning or hand-built events.

The existing record plane is not a complete transition history. `AssignTodo.apply`
overwrites the preceding order, `DoneTodoState.decide` clears the assignment, and
installation/withdrawal records retain no transition time. A read cannot recover
those facts. This slice must not invent historical owners or timestamps or claim
complete transition coverage. Restoring those facts needs the record-plane owner.

## Active shared-function conflicts

- #1912, updated 2026-10-19 17:02 UTC: turns script `board`, `on_record`, `recorded`,
  `posted` into Commander methods; also changes `BackVerb.report` and configuration.
  These functions remain untouched pending claude-code's ownership decision.
- #1756, updated 17:34 UTC: changes pattern-ID validation in BackVerb and Slop.
  Native event rendering does not change that validation.

Public posting is currently a script side effect after recording. Simply moving
that send after `Goals.send_back` would still leave a crash gap between stores.
An exactly-once claim needs the existing publication owner and a retained identity,
not another boolean on the widget or a replay of an uncertain verdict.

## AST evidence before editing

NRA `ModuleSyntaxIndex`, source parsed with Python 3.14: 379 production modules,
3,571 classes, 5,356 from-imports, 561 imports, 8,661 assignments, 5,267 annotated
assignments, 3,958 ifs, 4,105 comparisons, 31,947 calls, 43,152 attributes; zero parse
omissions. The initial Python 3.12 pass omitted three modules and was discarded.
NRA's own metaclass-registry dependency was loaded for the successful 3.14 pass;
the live package's newer registry is incompatible with that NRA checkout.

Semantically read: todos.py record families, TodoStore create/apply/assign/fire/
drop/fold/link, goal_actions.py HeldOrderGoalAction and problem creation/assignment,
goal_management.py change_problem/apply_problem/send_back, tools.py native order
and transcript reads, order_work.py, transcript_events.py, commander order/judge/
config and both active PR diffs. Dynamic bindings through tools and Commander
remain runtime obligations; syntax evidence does not prove them.

Catalog: IDEN-5 (script files split record authority), IMPL-5 (scripts reconstruct
event rendering), BOUND-8 (OrderCall drops its original event time).

## Verification and owned scratch

No installation or publication was performed. The stack Python ran this
checkout on PYTHONPATH through the actual CLI entrypoint on an isolated scratch bus.
Scratch owner pi-cn-phase5; lifecycle artifacts will be under this worktree's
`evidence/phase5/`. Preserve the concise transcript and remove disposable bus data
once accepted. `/home/ts/bin/agent-resource-check` is absent on this host. Host
headroom was 5.3 GiB available RAM and 26 GiB disk. The corrected focused test
command explicitly disables inherited xdist parallelism and repository-wide
coverage, and keeps its test scratch under this worktree.

## Implemented and demonstrated

The native tool family discovers CommsOrderStoryTool and CommsFleetStoryTool.
Todo declares its story collections; original events render their own lines.
SendBackVerdict composes verdict and fleet-story behavior through C3. OrderCall
retains the decoded start time instead of dropping it; its story distinguishes
that start from its current result. Peer acceptance names its peer message, not
fictitious words from our order. No kind-name switch selects story behavior.

The order-work reader consumed a frozen copy of this worker's actual saved native
session, including an actual read of order_story.py after the scratch order was
created. No event was authored and no saved input was replayed. The real CLI then
recorded acceptance, send-back/strike and operation delivery. Both stories are
pasted at every step in evidence/phase5/lifecycle.md. Two subsequent reads were
identical and left the Todo store mtime unchanged. The original check ran through
comms_order_done; its artifact was the actual textual CLI receipt, not an image.

This is source-backed CLI verification, not installed paired-runtime acceptance.
No Toad surface changed. The actual CLI verb is `tool --from`, not the plan's old
`invoke --from`. Full script deletion and public posting remain unfinished.

One send-back was refused before recording for using the banned word "retained";
continuation did not replay the already recorded acceptance. The stack environment
has no pytest. The development environment's first focused run passed five tests
but failed the inherited whole-repository coverage floor (48% versus 85%). The
corrected final bounded run disables that irrelevant coverage gate and uses one
worker: five passed, 72 deselected in 3.39 seconds. The only warning is the missing
pytest-timeout plugin; the shell timeout still bounded the run. Results and exact
source receipt are retained under evidence/phase5/.

After-edit NRA syntax census: 380 modules, 3,579 classes, zero parse omissions.
Story rendering lives on the original event owners and shared StoryEvent
contract; Todo and the native tools gather and sort their results. The shared
script functions involved in #1912 remain untouched.
