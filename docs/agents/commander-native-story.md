# Commander-native phase 5

Owner: pi-cn-phase5. Branch: feat/cn-phase5-story. Current repair order: P218.
Current main 5c4e7e3b4 is merged normally; #1912 is also merged into this branch
and remains a PR dependency until its own merge.

## Delivery checklist

- [x] Native chronological order story from original retained records (coverage limits below).
- [x] Native newest-first fleet story from original strikes and send-backs.
- [x] Real scratch-bus lifecycle, using public tools and actual unit records.
- [ ] Remove script board/narrative writers and roast; P218 repairs take priority.
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
  The explicit-context branch is now merged here; ownership is cleared, but the
  newer P218 ratchet repair supersedes the earlier deletion assignment.
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
pasted at every step in the PR body (removed from the tree as requested). Two subsequent reads were
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
script functions retain #1912's explicit context. The merge also keeps main's
record-owned delivery strikes instead of adding another script strike.

## P218 repair checkpoint

Published read-side repair: 6c7d459d5. No OrderPost implementation exists in this
checkpoint. Phase 3 has the exact source list of missing transition facts and
retains record-plane ownership. A future post must use verdict.previous, not the
result's holder, because automatic firing can release that holder in the same
transaction. Keep the publication insertion in that transaction; no second Todo
upsert or reconstructed history is acceptable.

The work reader now returns OrderWork or UnassignedOrderWork. Both the work tool
and story tool consume that same result; neither probes Todo.assignment. The
Todo store selects the actual assigned row by its existing keyed index. The
original float-or-null timestamp is decoded once through GeneratedTimestamp at
the tool-work read boundary. No sentinel, copied nullable timestamp or replacement
clock was added.

The journey is a typed Journey with declaration-owned steps, ordinary tool/action
records, top-level imports and shared result encoding. It does not turn typed
actions or deliveries into raw dictionaries and string-selected tool identities.
Mypy and Ruff pass for the rewritten journey. Fresh actual saved-session work,
acceptance, send-back and checked delivery succeeded on a new private scratch
bus. Five focused sanity tests passed in 5.08 seconds; none is claimed to prove
public posting or complete phase-5 acceptance.

The repair AST pass covers 382 production modules plus the rewritten journey:
3,627 declarations, 8,958 functions, 5,421 from-imports, 569 imports, 8,758 writes,
5,306 annotated writes, 3,965 ifs, 4,105 comparisons, 32,284 calls and 43,797
attributes, with zero parse omissions. NRA ModuleSyntaxIndex was reused. The
shared development Python lacks tree-sitter; the installed stack Python performed
this read-only census with NRA's own metaclass-registry dependency. No new parser
or package environment was created. Dynamic tool dispatch remains a runtime
obligation, not an AST proof.

Current owned disposable output: /home/ts/.cache/agent-scratch/pi-cn-phase5/,
including the new private bus and its copied session. Original user sessions and
UNKNOWN inputs are untouched. The old evidence/phase5 private bus remains an
unpublished historical proof source; it is not a second live installation.
