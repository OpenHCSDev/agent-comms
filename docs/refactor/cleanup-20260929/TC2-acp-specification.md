# TC2: The ACP specification's half of the boundary

**Repository:** Toad fork at `26aff491`. **Index:** [README.md](README.md). **Patterns:** BOUND-1, BOUND-7, IMPL-1, MEMB-3.

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
