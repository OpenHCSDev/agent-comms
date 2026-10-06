# TC2: The ACP specification's half of the boundary

**Repository:** Toad fork at `26aff491`. **Index:** [README.md](README.md). **Patterns:** BOUND-1, BOUND-7, IMPL-1, MEMB-3.

## Current disposition — 2026-10-05

The plan below is the original requirement, not an outstanding implementation
assignment. Toad208/202/209 were merged as43949ee; that merge is an ancestor of
PUBLIC447 main3dc801. The original installed launcher acceptance is retained in
[13-LIVE-INTEGRATION.md](13-LIVE-INTEGRATION.md): saved events and session/load
completed without new input. Later original installed native07/08 action/catalog
acceptance is recorded in [14-C0-SITE-LEDGER.md](14-C0-SITE-LEDGER.md).

Parent reread the current source boundary without importing a holder:

- `acp/api.py` derives request fields and response models from the official SDK;
  `acp/sdk_boundary.py` strictly validates SessionNotification/ToolCall at ingress.
  The old permissive `acp/protocol.py` is absent.
- `acp/status.py` owns StopReason and ToolCallStatus declaration families. Original
  specification spellings stay at this boundary; consumers take their behavior.
- `AgentDefinition.decode` uses FieldCodec at catalog ingress; `AgentKind.section`
  owns Store grouping. No `_agent_data` references were found in the reviewed
  ACP/agent/Store/tool-call paths.
- Original file-kind closure remains the shared declared208 boundary; this
  review does not introduce a second suffix list or rerun its installed gate.

The reviewed SDK/catalog/status files are byte-identical between PUBLIC447
main3dc801 and frozen43475d5. This targeted current source reconciliation does
not claim a new complete AST audit or full continuous workflow pass. No new
TC2 implementation or agent is needed; remaining workspace/continuous acceptance
stays with its existing owners in [16-CURRENT-S14-OWNERS.md](16-CURRENT-S14-OWNERS.md).

## Original plan

## What is wrong

T2 declared agent-comms' extension once and decoded it strictly. It left the ACP specification's own messages as they were, and its receipt said that half "stays". That was wrong: `acp/protocol.py`'s schema dicts are `total=False` with `extra_items=Any`, so they validate nothing, and the earlier `toad-acp-sdk-migration.md` plan traced a real false-clear bug to them (a required `PlanEntry.priority` treated as optional). TC2 supersedes that sentence of T2.

- **25 string-keyed reads in `acp/`,** such as `response["sessionId"]`, `modes["currentModeId"]` and `modes["availableModes"]` in the new `acp/agent_session.py`.
- **ACP values dispatched as strings:** `Conversation.agent_turn_over` on stop reasons (`end_turn`, `max_tokens`, `max_turn_requests`, `refusal`); `Conversation.on_acp_tool_call_update` and `tool_call.py::tool_call_header_content` on tool-call statuses, the latter with `failed` as well, so the two rosters differ.
- **Agent definitions as a raw private dict:** 11 reads of `self.agent._agent_data["…"]` from outside the agent, and `screens/store.py::compose_agents` dispatching on `agent["type"]` (`assistant`, `chat`, `coding`).
- **File kinds spelled twice:** image suffixes in `acp/prompt.py::build`, Markdown suffixes in `project_panel.py::_load_preview`.

## Target

- **Strict decoding of ACP messages** with the official `agent-client-protocol` SDK's typed models, as `toad-acp-sdk-migration.md` proposed; `protocol.py`'s schema dicts are deleted.
- **`StopReason` and `ToolCallStatus` families** with the specification's spellings, decided once at the boundary; one roster each.
- **`AgentDefinition`, a typed record** decoded once from the agent files, with an `AgentKind` family; no reads of `_agent_data` from outside the agent.
- **One declaration of accepted file kinds.**

## Crossings

`Conversation`'s two ACP dispatch sites change here, since the fix is ACP decoding; everything else in `Conversation` stays with T4.

## Done when

No string-keyed read of an ACP message remains in `acp/`; the three ACP dispatch sites and `compose_agents`' dispatch are families; `_agent_data` is private to the agent.

## Dispatch

> **`toad-tc2`:** Complete TC2 per `docs/refactor/cleanup/TC2-acp-specification.md`. Read `toad-acp-sdk-migration.md` first; adopt the SDK's models rather than extending the schema dicts.
