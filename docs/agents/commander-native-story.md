# Commander-native phase 5

Owner: pi-cn-phase5. Branch: feat/cn-phase5-story. Base: origin/main 011084fc5.

## Delivery checklist

- [ ] Native chronological order story from original retained records.
- [ ] Native newest-first fleet story from original strikes and send-backs.
- [ ] Real scratch-bus lifecycle, using public tools and actual unit records.
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

No installation or publication is authorized here. Use the existing stack Python
with this checkout on PYTHONPATH and the CLI entrypoint on an isolated scratch bus.
Scratch owner pi-cn-phase5; lifecycle artifacts will be under this worktree's
`evidence/phase5/`. Preserve the concise transcript and remove disposable bus data
once accepted. `/home/ts/bin/agent-resource-check` is absent on this host; use one
bounded background check, no parallel fleet or dependency rebuild.
