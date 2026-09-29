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

## Dispatch

> **`toad-tc1`:** Complete TC1 per `docs/refactor/cleanup/TC1-workspace-state.md`, starting with `session_presentation.py` and `transcript_publication.py`, after T9 in each.
