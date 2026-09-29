# C0: Enforce polymorphism

**Repositories:** both; the ratchet lives in agent-comms, and Toad runs the packaged version. **Index:** [README.md](README.md). **Patterns:** IMPL-1, IMPL-2, IMPL-3, TIME-9.

## Why

The plans so far enforced polymorphism surface by surface: each surface's guards protect its own result. Codebase-wide, the ratchet covers `type()` checks, chains, string-keyed reads, codec subclasses, foreign probes and class size, but not the two direct alternatives to polymorphism, and six merges in two days added one of them. Polymorphism cannot be required directly; its alternatives can be made impossible to add.

## Target

1. **Two ratchet measures,** counted per function, with nested functions measured on their own:
   - **`StringDispatch`:** one subject compared against three or more literals (`==`, `in`, `match`).
   - **`TypeSwitch`:** three or more `isinstance`, `type()` or class-pattern checks on one subject.

   Port them from the refactor-audit skill's census (`scripts/audit/measures.py`), where they were proven on known specimens: T7's `match` over command types and `_feed`'s `isinstance` chain register as type switches; `schema_to_widget`, the 146-line `slash_command` and `rpc_arguments` register as string dispatch; today's `slash_command`, rebuilt by T3, scores zero. Keep those specimens as the ratchet's tests.
2. **Seal mechanisms.** No codec subclass exists in either repository now, so the seal patch lands as written, rebased: `Sealed` on `FieldCodec`, `PendingRequests`, `ReadLedger`, `PiRpcChannel` and `ChildProcess`, and `WireValue` for types that own their wire form.
3. **Decide every existing site,** below. A **family** site is ours: its owner replaces the dispatch with a family whose members own the case. An **external** site dispatches on a taxonomy someone else owns (Python's types, Textual's actions, a file format); it stays, decided once at its boundary as one declared set or through the framework's own registry.

## Existing sites: agent-comms (`1f5b1f3a`)

| Site | Measure | Decides on | Kind | Owner | Decision |
|---|---|---|---|---|---|
| `acp_failure.py::_error_detail` | type switch | `value` | family | C3 | decode the error payload once; each shape renders its own detail |
| `field_codec.py::encode` | type switch | `value` | external | A2 builder | Python's type constructs; keep in the codec, or a handler family inside it |
| `field_codec.py::_decode` | type switch | `data` | external | A2 builder | as above |
| `goals.py::__post_init__` | string dispatch | `self.resolution` | family | C3 | goal resolutions are states: each carries its own data |
| `importing.py::read` | string dispatch | `source.suffix` | external | C1 | an imported transcript format and file suffixes: decode once into a family at import |
| `importing.py::_message` | string dispatch | `kind` | external | C1 | as above |
| `importing.py::_item` | string dispatch | `kind` | external | C1 | as above |
| `maintenance_barrier.py::current_unlocked` | string dispatch | `state['phase']` | family | C3 | barrier phases read from a raw dict: a lifecycle |
| `pi_events.py::apply` | string dispatch | `session.reason` | family | C1 | pi's compaction and stop reasons, decoded once into families |
| `pi_payloads.py::normalize` | type switch | `value` | family | C1 | pi payload shapes decoded once |
| `relationships.py::edit` | string dispatch | `action` | family | C3 | add, remove, update: a command family |
| `restart_queue.py::enqueue` | string dispatch | `key` | family | C2 | the environment carried across a restart, declared once |
| `restart_queue.py::step` | string dispatch | `reason` | family | C2 | restart failures compared by message text: a family |
| `selected_pi_summary_rpc.py::_summary_response` | type switch | `data` | family | C1 | the Summary*Data classes exist: each owns its response |
| `thread_status.py::allows_control` | string dispatch | `tool` | family | C3 | which commands a status allows: a capability on the commands |
| `threads.py::__post_init__` | string dispatch | `self.thinking_level` | family | C1 | pi's thinking levels, declared once with pi's spellings |
| `todos.py::_repo` | string dispatch | `piece` | external | none | path components: path semantics, keep |
| `tracked_turn.py::assistant_end` | string dispatch | `message.stop_reason` | family | C1 | pi's stop reasons, the same family as pi_events.py |

## Existing sites: Toad (`26aff491`)

| Site | Measure | Decides on | Kind | Owner | Decision |
|---|---|---|---|---|---|
| `acp/prompt.py::build` | string dispatch | `Path(path).suffix.lower()` | external | TC2 | image media types: declare the accepted types once |
| `conversation_markdown.py::resolve_tokens` | string dispatch | `child.type` | external | none | markdown-it token types: use the library's renderer rules |
| `danger.py::visitredirect` | string dispatch | `type` | external | none | bash redirect operators inside the bashlex visitor: keep, as one declared set |
| `screens/agent_modal.py::on_button_pressed` | string dispatch | `action` | family | T3 | button actions: Textual's @on selectors |
| `screens/store.py::compose_agents` | string dispatch | `agent['type']` | family | TC2 | agent kinds read from raw agent definitions |
| `widgets/conversation.py::check_action` | string dispatch | `action` | external | T4 | Textual action names: one declared registry per widget |
| `widgets/conversation.py::agent_turn_over` | string dispatch | `stop_reason` | family | TC2 | ACP stop reasons, decoded into a family |
| `widgets/conversation.py::on_acp_tool_call_update` | string dispatch | `status` | family | TC2 | ACP tool-call statuses, decoded into a family |
| `widgets/inline_message.py::inline_message` | string dispatch | `urlsplit(match.group('url')).scheme.lowe` | external | none | URL schemes: keep, as one declared set |
| `widgets/project_panel.py::_load_preview` | string dispatch | `self.path.suffix.lower()` | external | TC2 | file kinds: the same declaration as the image media types |
| `widgets/question.py::check_action` | string dispatch | `action` | external | T4 | Textual action names, as above |
| `widgets/side_bar.py::on_sidebar_action` | string dispatch | `event.action` | family | TC1 | sidebar placements: a small family |
| `widgets/terminal.py::_encode_mouse_event_sgr` | type switch | `event` | external | none | Textual's mouse event classes: keep |
| `widgets/tool_call.py::tool_call_header_content` | string dispatch | `status` | family | TC2 | ACP tool-call statuses, the same family |
| `work_preparation.py::retained_bytes` | type switch | `item` | external | none | Python primitive types for size accounting: keep |

## Guards

`StringDispatch` and `TypeSwitch` never increase in a touched file; mechanisms are sealed at import.

## Done when

Both measures are in the required ratchet and re-find their specimens; the seal patch is merged; every family site is removed by its owner; every external site is decided once at its boundary.

## Dispatch

> **`ac-c0`:** Complete C0 per `docs/refactor/cleanup/C0-enforce-polymorphism.md`: port the two measures into `agent_comms.debt_ratchet` with the specimens as tests, rebase and land the seal patch, and post the site table on the wire so each owner claims its rows.
