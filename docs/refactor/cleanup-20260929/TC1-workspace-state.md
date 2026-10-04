# TC1: Workspace state

**Repository:** Toad fork at `26aff491`. **Index:** [README.md](README.md). **Patterns:** IDEN-3, IMPL-10.

## What is wrong

The workspace feature line is the one place Toad's debt grew. Across two days, `None` checks rose by 46 and foreign probes by 18, almost all from these merges: **#129** (`workspace-bound`: +86 `None` checks, +29 probes), #142, #160 and #194. By file, weighted by `None` checks plus twice the probes:

| File | Weight | From |
|---|---|---|
| `session_presentation.py` | 75 | #129 |
| `transcript_publication.py` | 68 | #129, #142 |
| `screens/workspace.py` | 40 | #142 |
| `terminal_execution.py` | 15 | #129 |
| `transcript_source_preparation.py` | 12 | #160 |
| `widgets/project_tree_intent.py` | 11 | #129 |
| `widgets/viewport_body.py` | 10 | #142, #160 |
| `workspace_chrome.py` | 9 | #129 |

The pattern is the same throughout: workspace state kept as optional fields and probed from outside, instead of states that answer.

## Target

- **Explicit workspace states** (what is bound, loading, shown or detached) as a lifecycle family whose members answer the questions the probes ask.
- **Owners report:** code that reads another object's optional fields asks that object instead.
- `widgets/side_bar.py::on_sidebar_action` becomes a small placement family (from C0).

## Crossings

- **T9** owns every long condition in these files, including `session_presentation.py:226`, which waits on T4's `Conversation.is_untouched`. TC1 runs after T9 in shared files and never edits a chain.
- **T5** owns `transcript_history.py`, `comms_sidebar.py`, `comms_chat.py` and `goal_bar.py`; TC1 touches none of them.

## Done when

`None` checks and foreign probes in the eight files fall below their levels before #129.

## Current source check, 2026-10-04

The original threshold is not established. Parent used the existing refactor-audit
Repository/Census implementation at actual pre-129 parent436514d8 and merged
live419 source7c75c449. All218 baseline and288 current production modules parsed
under Python3.14 with zero omissions. Seven of the eight named files did not
exist before129; absence is explicitly recorded, not reported as a parsed zero
or evidence that their later implementations meet the threshold. The one
pre-existing viewport_body member has17 None checks/19 foreign probes now,
compared with4/11 before129. Those are syntax leads, not proof that each site
duplicates an authority or needs deletion.

Current session_presentation has21 None checks/4 foreign probes and the terminal
execution member has5/0. Source review confirms that TerminalOutcome and the
original child completion own exit meaning, while ACP absence remains its
external contract. NativeSessionSurface still retains its admitted owner/view
pair and OperationalSessionPresentation retains the mounted widget/editor
snapshot. Activate, retire, dispose and close interpret these lifetimes. Logical
WorkspaceSource selection and admitted native custody are distinct facts; a
closure must preserve that distinction rather than replace one with the other.
Heisenberg owns this complete existing surface and its warm-return/resource
consumers alongside the unfinished performance work. The useful420 sidebar
batch and native51 qualification are not held for this broader closure.

The compact determining source record is parent
`.artifacts/closure-source-check-20261004/TC1-current-source.json`. It records the
exact revisions and original measures; it is not behavioral or live acceptance.
Original installed A/B/A, editor/undo and shell detach qualifications remain at
their respective scopes. The original whole TC1 threshold and full continuous
workspace journey remain unproven.

## Dispatch

> **`toad-tc1`:** Complete TC1 per `docs/refactor/cleanup/TC1-workspace-state.md`, starting with `session_presentation.py` and `transcript_publication.py`, after T9 in each.

## Current terminal execution sub-surface, 2026-10-01

Verified at Toad7572b7b7 (included unchanged in271):
`terminal_execution.py::TerminalExecution` keeps process, task, PTY descriptor,
return code, startup error and release status independently. `ToolState.finished`
reconstructs completion from two nullable fields; `TerminalTool.present_execution`
interprets the execution's return code again. These are IMPL-10 and IDEN-3
ownership witnesses, not a claim that every optional external API field is debt.
The OS subprocess result and ACP nullable exit fields are external contracts;
decode their meaning once and project that contract at the ACP boundary.

Arendt owns this execution sub-surface and its ACP/controller/widget consumers.
Kepler contributes the actual installed PTY/ACP journey to the same draft.
They must create an isolated persistent worktree and draft before long work.
Heisenberg271 retains session/transcript/viewport/workspace ownership; shared
file changes require direct agreement. No new coordinator or parallel process
authority. This independent continuation does not modify the frozen receiver
release or repeat its accepted checks.

Target: the original execution owns lifecycle operations and completion;
widgets and ACP views derive from it. Preserve ANSI/output bounds, shell input,
resize, signal semantics, cancellation, release and detached-view continuity.
Use behavior-owning states rather than a second status cache, copied return
code, alias or nullable field combinations. Delete replaced logic in every
consumer. Preserve genuine weak-reference collection and external API shapes.

Acceptance: reuse the real application/controller/PTY journey for create,
output, normal exit, signal exit, failed startup, cancellation, release and
detach/reattach, with no leaked children. Verify the changed installed entrypoint;
focused tests alone are not readiness. Use bounded owned fixtures, no public
session effects or paid provider calls, and clean generated artifacts. Report
production deletions and exact remaining scope. This does not close the other
TC1 files or its original whole-surface threshold.
