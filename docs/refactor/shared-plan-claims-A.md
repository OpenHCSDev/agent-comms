# Shared plan claims: A dependency checkpoint

Owner: pi-plan-claims. Branch: feat/shared-plan-claims. Base: 011084fc5.
This checkpoint changes commander-native dependency declarations only. Shared
claims are the next, separate commit. Nothing is installed or merged here.

## Change and limit

`CutoverPhase.after` now names `RecordPlanePhase`, `ReviewQueuePhase` and
`ControlPlanePhase`. `StoryPhase` and `CommanderPagePhase` follow cutover, whose
prerequisites own their source records. No phase depends on either view.
`SkillPhase` continues to follow cutover.

**There is no declared executable commander parity gate.** The three requirements
in `plans/commander-native-plan.md:199-203` are prose: native owners on main, a
shadow session comparing native behaviour to scripts, and no private-state writes.
No phase or check implements that comparison. `PlanPhase.done()` reads order
closure, not parity. This change does not satisfy or encode the missing parity
prerequisite and does not authorize deleting the scripts.

Consequently there is no real parity-check output from which to enumerate failing
matrix rows for phases 3, 4 and 7, map each to a check class, or assign each
measured failure. Those results are **unavailable**, not passing. The commander
must identify an existing external check if one was intended, or commission its
implementation separately. No substitute parity check was invented in A.

## Actual Core output, before and after

Both reads used this checkout's `Todo.select`, `PlanPhase.ready` and
`PlanPhase.ordered` against a SQLite read-only connection to
`/home/ts/.agent-comms/todos.sqlite3`, selecting `creator='claude-code'`. Each
returned 214 retained orders. No synthetic completion state was used here.
This is the proposed Core source evaluating real retained state, not an installed
runtime update. "Next startable" is the same ready-and-not-ordered predicate
used by `PlanIdle.violations`.

Before:

```text
BEFORE: live commander claude-code; todos: 214
Core ready: record_plane, commander_page
Core next startable (ready, no open order): commander_page
record_plane done= False ordered= True after= retained_store open= P107,P167,P195,P197,P198,P204,P208,P213
review_queue done= True ordered= False after= record_plane open=
story done= True ordered= False after= review_queue open=
commander_page done= False ordered= False after= story open=
control_plane done= False ordered= True after= record_plane open= P100,P199
cutover done= False ordered= True after= review_queue,story,commander_page,control_plane open= P207
skill done= False ordered= False after= cutover open=
```

After:

```text
AFTER: live commander claude-code; todos: 214
Core ready: record_plane
Core next startable (ready, no open order): (none)
record_plane done= False ordered= True after= retained_store open= P107,P167,P195,P197,P198,P204,P208,P213
review_queue done= True ordered= False after= record_plane open=
control_plane done= False ordered= True after= record_plane open= P100,P199
cutover done= False ordered= True after= record_plane,review_queue,control_plane open= P207
story done= True ordered= False after= cutover open=
commander_page done= False ordered= False after= cutover open=
skill done= False ordered= False after= cutover open=
```

The retained state contradicts the initial description that nobody started the
story: Core reports `story` and `review_queue` done. Its unfinished native work
is in record plane and control plane. These are order-state observations, not
parity failures.

## Source ownership evidence

Reused refactor-audit's `audit.findings.Package.load` AST substrate at the base:
379 production modules, 476 test modules and 34 tool modules parsed, no omissions.
The first attempt to use NRA's parser could not import `tree_sitter` in the
available Python environment; the existing refactor-audit parser succeeded.

- `plan_phases.py` alone declares phase classes, dependency writes, completion,
  readiness and ordering decisions. No second dependency list was introduced.
- Production consumers: `standing_rules.py` (`PlanIdle`), `todos.py`
  (`ProblemOrder.serves` and serve command), `tools.py` (serve tool),
  `commander/order.py` (order parsing and serve verb).
- Test consumers at the base: `test_standing_rules.py` and `test_tool_replies.py`.
- Before: cutover depended on story and commander page; page depended on story.
  After: cutover depends on the three native phases; both views depend on cutover;
  no dependency names either view. Wire names and all consumer APIs are unchanged.
- Dynamic membership is derived through `DeclaredFamily.members_with`; static
  name enumeration does not prove absence of arbitrary external runtime mutation.
- Search for `parity` across production, tests, tools and CI found no commander
  comparison check. Unrelated Pi prompt-byte and entry-index parity tests do not
  compare commander behaviour.

## Verification

The existing native test environment for this checkout was verified and reused;
no runtime package was built or installed. One sequential, bounded test run:

```text
PYTHONPATH=src timeout 120 /home/ts/code/projects/agent-comms/.venv/bin/python -X faulthandler -m pytest --native-cache /home/ts/.cache/agent-comms/native -n 0 --no-cov -p no:cacheprovider -q tests/test_plan_phases.py tests/test_standing_rules.py -k 'cutover_waits or plan_idle'
2 passed, 16 deselected, 14 warnings in 4.63s
```

The new family test uses real checked orders and the serve tool. It verifies
cutover waits for each native prerequisite independently, unfinished views cannot
block cutover, and the views and skill become ready after cutover. The existing
PlanIdle test still passes. Warnings were an existing unknown pytest timeout
option and pytest cleanup trying to retire unrelated busy DQDock snapshot paths;
no manual cleanup of those paths was attempted.

`/home/ts/bin/agent-resource-check` is absent on this machine. Before this small
run, available RAM was 3.7 GiB and disk free was 26 GiB. No parallel workers or
large build was launched.

## Next identity choice

B will use one shared phase family with an unnamed parent per plan. Existing
commander-native phase names remain stable; the remaining-slop declarations get
unambiguous names derived from their classes. A plan parent groups discovery and
rendering without copying phase readiness/claim behaviour into a second family.
Markdown/board references identify declarations, never titles or new UUIDs.

Assumed: the missing parity comparison is reported, not invented; its absence
blocks parity acceptance and script deletion, but does not prevent reviewing this
dependency-only checkpoint. Both machines can write the approved shared claim
store. pi-cn-phase7 owns dispatch and will propose any checked-task capability;
this checkpoint does not fabricate runnable task bodies from phase names.
