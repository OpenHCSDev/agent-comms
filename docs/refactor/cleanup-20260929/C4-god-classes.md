# C4: The last god classes

**Repository:** agent-comms at `1f5b1f3a`. **Index:** [README.md](README.md). **Patterns:** IMPL-8, AGENT-4, AGENT-6.

Three classes remain over 500 lines, down from twelve two days ago: `HistoryViews` (`history_views.py`, 1,012 lines), `WireLog` (`wire_log.py`, 648) and `CommsAgent` (`acp.py`, 545).

- **`WireLog`** waits on D22's wire-log decision and is not part of this plan.
- **`HistoryViews` and `CommsAgent`:** re-measure at dispatch, since C1 to C3 will shrink them, then extract components that own state (IMPL-8). Never carve mixins (AGENT-6). Every extraction states the new-case edit count it reduces; one that only moves lines is relocation and is labelled as such. The class-size ratchet already stops both from growing.

## Done when

No class in `src/agent_comms/` except `WireLog` exceeds 500 lines, and each extraction's PR states its edit-count reduction.

## Dispatch

> **`ac-c4`:** Complete C4 per `docs/refactor/cleanup/C4-god-classes.md`, after C1 to C3. Re-measure first; components that own state only.
