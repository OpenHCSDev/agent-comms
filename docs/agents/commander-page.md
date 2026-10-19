# Commander page: delivered first slice

Issue #1495. Drafts: Core #1937 and Toad #1080. Owner: pi-cn-phase6.

## Delivered, not installed

`/commander <commander-thread>` opens the board in normal Toad. One collapsed line per held unit shows its problem number, plain problem text, deadline, recorded delivery state and native git/PR status. Overdue units come first. Click/Enter expands only that unit: full canonical order instructions, the actual latest checkout commit and up to three original recent turns, one line each.

Alarms show native stalled orders, recorded failed deliveries and retained refused sends. Alarm sources declare their reads; Toad has no alarm-kind switch. Refusals come from pi-connector's `SendRefusal.recent`, not from a UI log. **Core #1937 locally integrates dependency #1922; land that dependency first.** No GitHub PR was merged. Nothing was installed, relinked or restarted in the normal launcher.

This is the requested first slice, not full phase 6: strikes, review queue, story, sidebar order decorations, turn/file drill-down and predictions remain outside this delivery.

## Owners, consumers and deletions

- `TodoStore.held_for_commander` selects indexed held rows; `Todo.held().owner_identity` supplies nominal unit identity; `Todo.overdue` supplies overdue meaning. `CommanderModel` retains those types. It does not inspect kind/name strings to recover semantics.
- `ProblemOrder.instructions` supplies the complete order body. No second order renderer was written in Toad.
- `ProgressSource.progress` in `todos.py:333` now owns the sole progress selection algorithm, moved out of `OrderProgress.read`. The latter delegates to it. `StatusProgressSource` inherits that selection and suppresses acceptance-distance measurement: reading the page cannot execute an order's arbitrary acceptance check. Existing measured native-tool behavior remains on `ProgressSource`.
- `ProgressSource.latest_commit` reads one actual commit only for expanded cards. `OrderProgress.stalled` supplies stall policy. GitHub/read failures are `UnreadProgress`, not a false no-commit state.
- `TranscriptEvent.turn_line` and `TurnDigest.line` own recent-work summaries. Assistant and tool-start declarations contribute; prompts do not swamp the card with wire envelopes. The model's earlier generic imported-body rendering was deleted.
- `ReadCommanderIdentity` resolves the command spelling to `ThreadIncarnation` inside the existing observation process. Toad performs no registry or Todo read. Intentional typed read refusals return through the existing `ReadRefused` boundary instead of killing the observer.
- `ObserveCommander` and `CommanderReading` use the existing interest/process lifetime. `WireRevision` watches Todo main/WAL files plus relevant existing sources and the existing expiry tick. Closing the page removes its interest; there is no new polling timer or store.
- Toad's `CommanderCommand` is discovered from its declaration, with no command roster or MainScreen edit. `CommanderUnit` extends `Collapsible`; its existing title and explicit detail widgets are updated in place. A real run caught an incorrect broad `Static` lookup that updated the title; that lookup was deleted.

One selection declaration, confirmed with:

```
rg -n 'members = sorted\(OrderProgress.members_with' src/agent_comms
# todos.py:335 only
```

## Overlap and source evidence

All open Core and Toad PR file lists were read before writing. #1903/#1873 concern launch/runtime classes not changed here. P603 #1757 was already merged. #1912 and #1756 diffs were read; their root-context and pattern-ID classes are not changed here. Same-file changes in `todos.py` are separate functions/classes. Recent Toad #1070 changes MainScreen and existing command classes; this slice adds a new command and screen instead. #1917 changes imported-message/UserTranscript behavior, not the event/turn-summary methods added here. `plan_phases.py` was not edited.

Read the refactor-audit skill and pattern catalog. Reused its `Package` AST parser and overlay for both complete production roots, before/after source revisions, with zero parse omissions. Owners, imports, inheritance, direct consumers and function checks are recorded in `/home/ts/.cache/agent-scratch/pi-cn-phase6/owner-consumers.txt`. Dynamic family discovery, MRO dispatch and aliases were read semantically; the AST listing does not claim complete dynamic resolution. NRA is not installed on this machine.

Relevant patterns: BOUND-2/BOUND-8 (retain Core's domain records rather than mapped shapes), MEMB-1 (derive command/alarm membership from declarations), IMPL-12 (one progress selector, inherited source capability). The final overlay adds no string/type dispatch switches or raw-record shapes. Its unused-module heuristic cannot see the pickled observation interest crossing the paired repository boundary.

## Actual application acceptance

Verified implementation pair: **Core e248a6ace / Toad 292bdaa5**, using the existing installed stack Python/dependencies with the two source checkouts on `PYTHONPATH`. This is a source-paired real Toad run, not a claim that the normal installed launcher contains the feature.

Ran `tools/commander_page_journey.py` in **plain st on isolated Xvfb :186**, a real `ToadApp`, real observation child and scratch bus. It opened a real bus conversation and submitted `/commander claude-code` through the normal prompt/local-command boundary. It clicked a real Collapsible title, recorded delivery through the real order-completion tool, observed the page update without reopening, and returned to the identical underlying conversation.

The scratch bus contains copies of actual registered units and complete-line cuts of their real saved session files, not fixture transcripts or hand-built events. Orders were created through `CommsTodoCreateTool`. The refusal was one actual `CommsSendTool` attempt to an absent target, retained by the send owner. No provider/agent was started and no original session, registry or uncertain input was modified. Snapshot provenance is in `physical-4/sources.txt`.

Artifacts under `/home/ts/.cache/agent-scratch/pi-cn-phase6/physical-4/`:

- `level-0.png`, `level-0.txt`: physically displayed collapsed board and alarms.
- `level-1.png`, `level-1.txt`: actual expanded card, commit and original turns; second unit still collapsed.
- `live-delivery.png`, `live-delivery.txt`: same open card changes to Delivered, OVERDUE disappears and its stall alarm clears; the refusal remains.
- `result.txt`: successful continuous journey receipt.

Text below is excerpted from the actual composed terminal strips; the files contain the complete screen.

### Level 0

```
Commander claude-code | 2 units | Overdue first · Enter/click a unit to expand · Esc to return
▶ pi-cn-phase2 | OVERDUE P1: Show the real order in the commander page | due 14:23:01 | Open; no commit since the order
▶ pi-cn-phase5 | P2: Show the real order in the commander page | due 14:54:01 | Open; no commit since the order
Alarms
STALLED pi-cn-phase2 P1: no commit since the order
REFUSED SEND #1 claude-code -> unregistered-page-target: Target 'unregistered-page-target' is not a registered thread.
```

### Level 1, selected screen lines

```
▼ pi-cn-phase2 | OVERDUE P1: Show the real order in the commander page | due 14:23:01 | Open; no commit since the order
  Problem: P1: Show the real order in the commander page
  Order: commander-page-pi-cn-phase2
  1. Render the canonical order owner without copying its state
  Latest checkout commits:
  P1: 69688af8d Merge remote-tracking branch 'origin/main' into feat/cn-phase2-design
  Recent turns (newest first):
  eb8bb409-f882-4e9b-80f2-efd20829b639: Run gh pr edit 1931 -R OpenHCSDev/agent-comms ...
  35561dbb-3227-4076-bda3-078c45dfdf7b: Run gh pr diff 1587 -R OpenHCSDev/agent-comms; ...
▶ pi-cn-phase5 | P2: Show the real order in the commander page | due 14:54:01 | Open; no commit since the order
```

## Checks and retained output

Focused batch covers commander model, read-only progress, refusals and native/external turn readers. Three pre-existing turn-reader fixtures lacked required positions/timestamps; updated those fixtures without weakening their assertions. The final batch passed **40 checks in 31.90 seconds**, including the identity/refusal boundary check. Commands run in the background under `timeout 300 ... -X faulthandler`, with pytest default parallelism/whole-suite coverage disabled for the small selected batch. The environment lacks pytest-timeout, producing one configuration warning; the outer timeout remains enforced. New-file Ruff and both `git diff --check` checks pass. CI/full-suite acceptance was not requested or claimed.

Owned disposable output: `/home/ts/.cache/agent-scratch/pi-cn-phase6`. Keep `physical-4` screen/source receipts while the drafts await review. Superseded physical runs and test databases were retired after checking actual borrowers; the isolated Xvfb/st processes were stopped. Source and handoffs stay in `/home/ts/wt/pi-cn-phase6--P1` and `--P2`. The standard resource-check executable is absent here; measured 3.1 GiB available RAM and 26 GiB disk before the bounded run. One physical UI was used; no parallel agent was launched.
