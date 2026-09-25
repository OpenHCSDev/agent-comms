"""Declarative tool surface shared by process and protocol adapters."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from typing import Literal

from .declarations import (
    ChannelSort,
    MessageType,
    SavedView,
    ThreadRole,
    ThreadSort,
    ViewKind,
    ViewMatch,
    ViewPredicate,
    is_channel_target,
)
from .operations import Comms, ForkSpec, TagAction
from .tool_output import MAX_INLINE_OUTPUT_BYTES, materialize_oversized_output

JsonObject = dict[str, object]
ToolHandler = Callable[[Comms, Mapping[str, object]], JsonObject]


@dataclass(frozen=True, slots=True)
class ToolParameter:
    name: str
    kind: Literal["string", "boolean", "array"]
    description: str
    required: bool = True
    default: object | None = None
    choices: tuple[str, ...] = ()
    context_value: Literal["subject", "actor"] | None = None

    def schema(self) -> JsonObject:
        schema: JsonObject = {"type": self.kind, "description": self.description}
        if self.kind == "array":
            schema["items"] = {"type": "string"}
        if self.choices:
            schema["enum"] = list(self.choices)
        if not self.required and self.default is not None:
            schema["default"] = self.default
        return schema


@dataclass(frozen=True, slots=True)
class ToolDeclaration:
    name: str
    label: str
    description: str
    parameters: tuple[ToolParameter, ...]
    handler: ToolHandler
    context: str | None = None
    action_label: str | None = None
    action_order: int = 0

    def schema(self) -> JsonObject:
        schema: JsonObject = {
            "name": self.name,
            "label": self.label,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": {parameter.name: parameter.schema() for parameter in self.parameters},
                "required": [parameter.name for parameter in self.parameters if parameter.required],
                "additionalProperties": False,
            },
        }
        if self.context is not None:
            schema["context"] = self.context
            schema["action_label"] = self.action_label or self.label
            schema["action_order"] = self.action_order
            schema["context_bindings"] = {
                parameter.name: parameter.context_value
                for parameter in self.parameters
                if parameter.context_value is not None
            }
        return schema

    def invoke(self, comms: Comms, raw_arguments: Mapping[str, object]) -> JsonObject:
        declared = {parameter.name: parameter for parameter in self.parameters}
        unknown = sorted(set(raw_arguments) - set(declared))
        if unknown:
            raise ValueError(f"Unknown arguments for {self.name}: {', '.join(unknown)}")
        arguments: JsonObject = {}
        for parameter in self.parameters:
            if parameter.name not in raw_arguments:
                if parameter.required:
                    raise ValueError(
                        f"Missing required argument {parameter.name!r} for {self.name}."
                    )
                arguments[parameter.name] = parameter.default
                continue
            value = raw_arguments[parameter.name]
            expected = {"string": str, "boolean": bool, "array": list}[parameter.kind]
            if not isinstance(value, expected):
                raise ValueError(f"Argument {parameter.name!r} must be {parameter.kind}.")
            if parameter.kind == "array" and (
                not isinstance(value, list) or any(not isinstance(item, str) for item in value)
            ):
                raise ValueError(f"Argument {parameter.name!r} must contain strings.")
            if parameter.choices and value not in parameter.choices:
                raise ValueError(
                    f"Argument {parameter.name!r} must be one of: " + ", ".join(parameter.choices)
                )
            arguments[parameter.name] = value
        return self.handler(comms, arguments)


def _threads(comms: Comms, arguments: Mapping[str, object]) -> JsonObject:
    return {"threads": list(comms.list_threads(active_only=bool(arguments["active_only"])))}


def _rename_self(comms: Comms, arguments: Mapping[str, object]) -> JsonObject:
    result = comms.rename_self(str(arguments["new_name"]))
    return {"previous": result.previous, "current": result.current, "changed": result.changed}


def _set_project(comms: Comms, arguments: Mapping[str, object]) -> JsonObject:
    result = comms.set_project_self(str(arguments["path"]))
    return {
        **asdict(result),
        "instruction": (
            "Project saved. End this turn now; the runtime automatically resumes this same "
            "conversation in the new project with refreshed tools and context."
            if result.changed
            else "This is already your project directory."
        ),
    }


def _send(comms: Comms, arguments: Mapping[str, object]) -> JsonObject:
    message = comms.send_message(
        str(arguments["from"]),
        str(arguments["to"]),
        str(arguments["body"]),
        MessageType(str(arguments["type"])),
    )
    return {"id": message.message_id, "message": message.to_wire()}


def _inbox(comms: Comms, arguments: Mapping[str, object]) -> JsonObject:
    thread = str(arguments["thread"])
    goal_id = arguments.get("goal_id")
    wait_for = arguments.get("wait_for")
    assert wait_for is None or isinstance(wait_for, list)
    if bool(goal_id) != bool(wait_for):
        raise ValueError("Provide goal_id and wait_for together for standby review.")
    review = (
        comms.goal_input_review(thread, str(goal_id), wait_for) if goal_id and wait_for else None
    )
    messages = [message.to_wire() for message in comms.inbox(thread)]
    unresolved = comms.unresolved_inputs(thread)
    response: JsonObject = {
        "messages": messages,
        "acknowledged": 0,
        "unresolved_inputs": unresolved,
        **({"standby_review": review} if review is not None else {}),
    }
    # Reserve room for the eventual ACK count before performing that mutation.
    # Omitted messages must remain unread even if artifact publication fails.
    result_file = materialize_oversized_output(
        comms.root, response, inline_limit=MAX_INLINE_OUTPUT_BYTES - 64
    )
    if result_file is not None:
        bounded: JsonObject = {
            "messages": [],
            "acknowledged": 0,
            "unresolved_inputs": [],
            "complete": False,
            "ackDeferred": bool(arguments["ack"]),
            "counts": {"messages": len(messages), "unresolved_inputs": len(unresolved)},
            "result_file": str(result_file),
            "instruction": (
                "Payload arrays are omitted, not empty. The file is a complete public result "
                "snapshot, not current authority. Read it selectively with read offset/limit "
                "or a JSON query; do not "
                "dump the whole file into context. No messages were acknowledged. UNKNOWN "
                "inputs are unchanged. Review full relevant messages before using any "
                "standby reviewed_inputs from the file."
            ),
        }
        if review is not None:
            bounded["standby_review"] = {
                "complete": False,
                "counts": {
                    key: len(review[key])
                    for key in ("messages", "already_reviewed_inputs", "excluded_inputs")
                },
            }
        return bounded
    response["acknowledged"] = comms.acknowledge(thread) if arguments["ack"] else 0
    return response


def _fork(comms: Comms, arguments: Mapping[str, object]) -> JsonObject:
    tags = frozenset(tag.strip() for tag in str(arguments["tags"]).split(",") if tag.strip())
    prompt = arguments["prompt"]
    child = comms.fork(
        ForkSpec(
            name=str(arguments["name"]),
            parent=str(arguments["parent"]),
            task=str(arguments["task"]),
            tags=tags,
            prompt=str(prompt) if prompt is not None else None,
        )
    )
    return {"forked": child.name, "pid": child.pid}


def _start(comms: Comms, arguments: Mapping[str, object]) -> JsonObject:
    return asdict(comms.start(str(arguments["name"])))


def _ack(comms: Comms, arguments: Mapping[str, object]) -> JsonObject:
    target = arguments["target"]
    acknowledged = comms.acknowledge(
        str(arguments["thread"]), str(target) if target is not None else None
    )
    return {"acknowledged": acknowledged}


def _dismiss(comms: Comms, arguments: Mapping[str, object]) -> JsonObject:
    """Dismiss channel pings addressed to other threads, ending the turn quietly."""
    thread = str(arguments["thread"] or _executing_thread())
    target = str(arguments["target"]).strip()

    if not target:
        raise ValueError("A channel target is required.")
    name = comms.registry.require(thread).name
    bus_messages = comms.bus.inbox(name, target)
    if not bus_messages:
        return {
            "thread": name,
            "target": target,
            "acknowledged": 0,
            "mentioned": (name,),
            "dismissed": True,
        }
    mentioned: tuple[str, ...] = ()
    for message in bus_messages:
        if not is_channel_target(message.target) and message.sender_role is not ThreadRole.AGENT:
            continue
        if message.target != target and message.sender != target:
            continue
        if message.mentions:
            mentioned = tuple(
                dict.fromkeys(item.thread for item in message.mentions if item.thread != name)
            )
            break
    else:
        mentioned = (name,)
    acknowledged = comms.acknowledge(name, target)
    return {
        "thread": name,
        "target": target,
        "acknowledged": acknowledged,
        "mentioned": mentioned,
        "dismissed": True,
    }


def _stop(comms: Comms, arguments: Mapping[str, object]) -> JsonObject:
    name = str(arguments["name"])
    comms.stop(name)
    return {"stopped": name}


def _archive(comms: Comms, arguments: Mapping[str, object]) -> JsonObject:
    name = str(arguments["name"])
    comms.archive(name)
    return {"archived": name}


def _delete(comms: Comms, arguments: Mapping[str, object]) -> JsonObject:
    result = comms.delete(str(arguments["name"]))
    return {
        "deleted": result.name,
        "messages_removed": result.messages_removed,
        "markers_removed": result.markers_removed,
        "activity_events_removed": result.activity_events_removed,
        "runtime_removed": result.runtime_removed,
        "ledger_references_removed": result.ledger_references_removed,
        "detached_children": list(result.detached_children),
    }


def _executing_thread() -> str:
    import os

    name = os.environ.get("PI_AGENT_ID") or os.environ.get("AGENT_COMMS_THREAD")
    if not name:
        raise ValueError("Goal updates require an executing thread identity.")
    return name


def _set_goal(comms: Comms, arguments: Mapping[str, object]) -> JsonObject:
    goal = comms.update_goal(
        _executing_thread(),
        "set",
        text=str(arguments["text"]),
    )
    return {
        "goal": asdict(goal) if goal else None,
        "instruction": (
            "Persistent goal activated. Work toward it and report progress with comms_goal."
        ),
    }


def _goal(comms: Comms, arguments: Mapping[str, object]) -> JsonObject:
    wait_for = arguments.get("wait_for")
    assert wait_for is None or isinstance(wait_for, list)
    reviewed_inputs = arguments.get("reviewed_inputs")
    assert reviewed_inputs is None or isinstance(reviewed_inputs, list)
    name = _executing_thread()
    comms.update_goal(
        name,
        str(arguments["status"]),
        goal_id=str(arguments["goal_id"]),
        expected_status="active",
        progress=str(arguments["progress"]),
        model_report=True,
        wait_for=wait_for or (),
        reviewed_inputs=reviewed_inputs or (),
    )
    goal, execution = comms.goal_snapshot(name)
    return {
        "goal": asdict(goal) if goal else None,
        "goal_execution": asdict(execution) if execution else None,
    }


def _resume_goal(comms: Comms, arguments: Mapping[str, object]) -> JsonObject:
    name = _executing_thread()
    goal_id = str(arguments["goal_id"])
    current = comms.registry.require(name).goal
    if current is None or current.id != goal_id or current.status != "paused":
        # A blocked goal may have an unresolved paid attempt. Only the
        # authenticated human-recovery path can decide that disposition.
        raise ValueError("This goal cannot be resumed; refresh its state.")
    pause = comms.goal_pause(name)
    if pause is None:
        raise ValueError(
            "Pause source is unavailable; the owner must resume through the goal controls."
        )
    if pause.owner_instruction is not None:
        raise ValueError(pause.owner_instruction)
    progress = str(arguments["progress"])
    goal = comms.update_goal(
        name,
        "active",
        goal_id=goal_id,
        expected_status=current.status,
        expected_goal=current,
        progress=progress,
    )
    if goal is None or not goal.active or goal.progress != progress:
        raise ValueError("Goal changed during resume; refresh its state.")
    return {"goal": asdict(goal)}


def _edit_goal(comms: Comms, arguments: Mapping[str, object]) -> JsonObject:
    name = _executing_thread()
    goal_id = str(arguments["goal_id"])
    current = comms.registry.require(name).goal
    if current is None or current.id != goal_id:
        raise ValueError("This goal was replaced or cleared; refresh its state.")
    goal = comms.update_goal(
        name,
        "edit",
        text=str(arguments["text"]),
        goal_id=goal_id,
        expected_goal=current,
    )
    return {"goal": asdict(goal) if goal else None}


def _goal_history(comms: Comms, arguments: Mapping[str, object]) -> JsonObject:
    requested = str(arguments["goal_id"]).strip()
    entries = comms.goal_history(_executing_thread(), goal_id=requested or None)
    return {"history": [asdict(entry) for entry in entries]}


def _collaboration(comms: Comms, arguments: Mapping[str, object]) -> JsonObject:
    relationship = comms.relationships.edit(
        _executing_thread(),
        str(arguments["action"]),
        str(arguments["peer"]),
        str(arguments["note"]),
    )
    return {"collaboration": asdict(relationship) if relationship else None}


def _collaborations(comms: Comms, arguments: Mapping[str, object]) -> JsonObject:
    owner = _executing_thread()
    snapshot = comms.relationships.snapshot(owner)
    group = next(group for group in snapshot.groups if group.key == "collaborating")
    result: JsonObject = {
        "collaborations": [
            asdict(edge) for edge in snapshot.explicit_collaborations
        ]  # Legacy explicit declarations remain independently editable.
    }
    if any(entry.goal_contacts for entry in group.entries):
        result["visible_collaborators"] = [
            {
                "peer": entry.target,
                "available": entry.available,
                "sources": entry.sources,
                "detail": entry.detail,
                "goal_contacts": [asdict(contact) for contact in entry.goal_contacts],
            }
            for entry in group.entries
        ]
    if snapshot.unresolved_goal_mentions:
        result["unresolved_goal_mentions"] = [
            asdict(row) for row in snapshot.unresolved_goal_mentions
        ]
    return result


def _tags(comms: Comms, arguments: Mapping[str, object]) -> JsonObject:
    tags = TagAction(str(arguments["action"])).apply(
        comms, str(arguments["name"]), str(arguments["new_name"])
    )
    return {"tags": sorted(tags)}


def _tag_set(value: object) -> frozenset[str]:
    return frozenset(tag.strip() for tag in str(value).split(",") if tag.strip())


def _thread_tags(comms: Comms, arguments: Mapping[str, object]) -> JsonObject:
    thread = comms.update_tags(
        str(arguments["thread"] or _executing_thread()),
        add=_tag_set(arguments["add"]),
        remove=_tag_set(arguments["remove"]),
    )
    return {"thread": thread.name, "tags": sorted(thread.tags)}


def _channels(comms: Comms, arguments: Mapping[str, object]) -> JsonObject:
    return {
        "channels": [view.to_wire() for view in comms.channel_views()],
        "views": [view.to_wire() for view in comms.saved_views().values()],
        "order": comms.channel_catalog.list_order.value,
    }


def _set_channel(comms: Comms, arguments: Mapping[str, object]) -> JsonObject:
    channel = comms.set_channel(str(arguments["name"]), _tag_set(arguments["tags"]))
    return channel.to_wire()


def _delete_channel(comms: Comms, arguments: Mapping[str, object]) -> JsonObject:
    comms.delete_channel(str(arguments["name"]))
    return _channels(comms, {})


def _set_channel_metadata(comms: Comms, arguments: Mapping[str, object]) -> JsonObject:
    parent = str(arguments["parent"]).strip() or None
    return comms.set_channel_metadata(
        str(arguments["name"]), parent=parent, archived=bool(arguments["archived"])
    ).to_wire()


def _set_view(comms: Comms, arguments: Mapping[str, object]) -> JsonObject:
    return comms.set_saved_view(
        SavedView(
            str(arguments["name"]),
            ViewKind(str(arguments["kind"])),
            ViewPredicate(ViewMatch(str(arguments["match"])), _tag_set(arguments["tags"])),
        )
    ).to_wire()


def _delete_view(comms: Comms, arguments: Mapping[str, object]) -> JsonObject:
    comms.delete_saved_view(str(arguments["name"]))
    return _channels(comms, {})


def _sort_channel(comms: Comms, arguments: Mapping[str, object]) -> JsonObject:
    return comms.set_channel_sort(
        str(arguments["name"]), ThreadSort(str(arguments["order"]))
    ).to_wire()


def _sort_channels(comms: Comms, arguments: Mapping[str, object]) -> JsonObject:
    order = comms.set_channel_order(ChannelSort(str(arguments["order"])))
    return {"order": order.value}


def _pin_channel(comms: Comms, arguments: Mapping[str, object]) -> JsonObject:
    return comms.set_channel_pinned(str(arguments["name"]), bool(arguments["pinned"])).to_wire()


def _pin_thread(comms: Comms, arguments: Mapping[str, object]) -> JsonObject:
    channel = str(arguments["channel"])
    comms.set_thread_pinned(channel, str(arguments["name"]), bool(arguments["pinned"]))
    canonical = channel if channel.startswith("#") else f"#{channel}"
    return next(view.to_wire() for view in comms.channel_views() if view.channel.name == canonical)


def _model(comms: Comms, arguments: Mapping[str, object]) -> JsonObject:
    """Change this thread's model or another thread's model for future turns."""
    thread_name = str(arguments["thread"] or _executing_thread())
    model = str(arguments["model"]).strip()
    thread = (
        comms.set_thread_model(thread_name, model) if model else comms.registry.require(thread_name)
    )
    thinking_level = str(arguments.get("thinking_level") or "").strip()
    if thinking_level:
        thread = comms.set_thread_thinking_level(thread.name, thinking_level)
    return {
        "thread": thread.name,
        "model": thread.model,
        "thinking_level": thread.thinking_level,
    }


TOOLS = (
    ToolDeclaration(
        "comms_pin_channel",
        "Pin Channel",
        "Persist whether a channel is pinned. Pinned channels sort before unpinned channels; "
        "the selected channel sort remains the secondary order.",
        (
            ToolParameter("name", "string", "Channel name"),
            ToolParameter("pinned", "boolean", "True to pin; false to unpin"),
        ),
        _pin_channel,
    ),
    ToolDeclaration(
        "comms_pin_thread",
        "Pin Thread in Channel",
        "Persist whether a member is pinned within one channel. Pins do not change membership "
        "or ordering in other channels. The selected member sort remains the secondary order.",
        (
            ToolParameter("channel", "string", "Channel containing the thread"),
            ToolParameter("name", "string", "Member thread name"),
            ToolParameter("pinned", "boolean", "True to pin; false to unpin"),
        ),
        _pin_thread,
    ),
    ToolDeclaration(
        "comms_model",
        "Change Thread Model",
        "Change this thread's model, or another thread's model, for its future turns. "
        "Optionally change its thinking level too. Applies from that thread's next turn.",
        (
            ToolParameter(
                "thread",
                "string",
                "Thread to change (defaults to the executing thread)",
                required=False,
                default=None,
                context_value="subject",
            ),
            ToolParameter("model", "string", "Provider/model identifier"),
            ToolParameter(
                "thinking_level",
                "string",
                "Optional Pi thinking level",
                required=False,
                default="",
                choices=("off", "minimal", "low", "medium", "high", "xhigh", "max"),
            ),
        ),
        _model,
    ),
    ToolDeclaration(
        "comms_sort_channels",
        "Sort Channels",
        "Persist channel-list ordering independently of member ordering. "
        "Pinned channels stay first, with built-in channels first within each pin group.",
        (
            ToolParameter(
                "order",
                "string",
                "Channel list order",
                choices=tuple(order.value for order in ChannelSort),
            ),
        ),
        _sort_channels,
    ),
    ToolDeclaration(
        "comms_tags",
        "Manage tags",
        "List, create, rename, or delete tags. Renaming/deleting updates thread assignments "
        "and channel filters. Uncovered tags automatically have a channel.",
        (
            ToolParameter(
                "action",
                "string",
                "Operation",
                required=False,
                default="list",
                choices=tuple(action.value for action in TagAction),
            ),
            ToolParameter("name", "string", "Tag name", required=False, default=""),
            ToolParameter(
                "new_name", "string", "Replacement tag for rename", required=False, default=""
            ),
        ),
        _tags,
    ),
    ToolDeclaration(
        "comms_thread_tags",
        "Assign thread tags",
        "Add/remove tags on a thread. Omit thread to manage your own tags. Channel membership "
        "and routing update immediately without restarting or creating an executor.",
        (
            ToolParameter("thread", "string", "Thread name or alias", required=False, default=""),
            ToolParameter(
                "add", "string", "Comma-separated tags to add", required=False, default=""
            ),
            ToolParameter(
                "remove", "string", "Comma-separated tags to remove", required=False, default=""
            ),
        ),
        _thread_tags,
    ),
    ToolDeclaration(
        "comms_channels",
        "List channels",
        "List named tag views and their current member threads; #any includes everyone.",
        (),
        _channels,
    ),
    ToolDeclaration(
        "comms_set_channel",
        "Configure legacy channel audience",
        "Create/update a compatibility routable OR-union of tags. Exact one-tag channels "
        "remain independently visible and retain target-owned history.",
        (
            ToolParameter("name", "string", "Channel name, e.g. #engineering"),
            ToolParameter("tags", "string", "One or more comma-separated tags"),
        ),
        _set_channel,
    ),
    ToolDeclaration(
        "comms_set_channel_metadata",
        "Set channel presentation",
        "Set presentation-only parent and archive state. These fields never alter routing, "
        "membership, history, unread state, or delivery.",
        (
            ToolParameter("name", "string", "Exact channel name"),
            ToolParameter("parent", "string", "Parent channel; empty clears the parent"),
            ToolParameter("archived", "boolean", "Whether to hide from active presentation"),
        ),
        _set_channel_metadata,
    ),
    ToolDeclaration(
        "comms_set_view",
        "Set non-routable view",
        "Create/update a typed participant or activity projection. Views cannot receive "
        "messages and never own channel history.",
        (
            ToolParameter("name", "string", "View name without #"),
            ToolParameter(
                "kind",
                "string",
                "Projection kind",
                choices=tuple(kind.value for kind in ViewKind),
            ),
            ToolParameter(
                "match",
                "string",
                "Tag predicate",
                choices=tuple(match.value for match in ViewMatch),
            ),
            ToolParameter("tags", "string", "One or more comma-separated tags"),
        ),
        _set_view,
    ),
    ToolDeclaration(
        "comms_delete_view",
        "Delete non-routable view",
        "Delete a saved projection without changing tags, channels, messages, or threads.",
        (ToolParameter("name", "string", "Saved view name"),),
        _delete_view,
    ),
    ToolDeclaration(
        "comms_delete_channel",
        "Delete channel view",
        "Remove a named view, preserving tags and threads. Uncovered tags regain automatic views.",
        (ToolParameter("name", "string", "Named channel to remove"),),
        _delete_channel,
    ),
    ToolDeclaration(
        "comms_sort_channel",
        "Sort channel members",
        "Persist one channel's member ordering, shared by all attached views.",
        (
            ToolParameter("name", "string", "Channel name"),
            ToolParameter(
                "order",
                "string",
                "Member ordering",
                choices=tuple(order.value for order in ThreadSort),
            ),
        ),
        _sort_channel,
    ),
    ToolDeclaration(
        "comms_set_project",
        "Change Project",
        "Change your own persistent project directory and synchronize attached clients. "
        "Accepts an existing absolute path, ~ path, or path relative to your current project. "
        "Preserves this thread and its conversation. After changing, end your turn; "
        "the runtime automatically continues in the new project.",
        (ToolParameter("path", "string", "New project directory"),),
        _set_project,
    ),
    ToolDeclaration(
        "comms_set_goal",
        "Set persistent goal",
        "Set or replace your own persistent goal. Use this when the user asks you to start, "
        "set, or pursue a goal autonomously.",
        (ToolParameter("text", "string", "Persistent objective to pursue"),),
        _set_goal,
    ),
    ToolDeclaration(
        "comms_goal",
        "Goal progress",
        "Update goal progress; complete it or block it when user input is needed. "
        "Use standby with explicit wait_for thread names when waiting for delegated work. "
        "The goal remains active, but only a direct message from a named dependency or a user "
        "follow-up starts its next turn. Do not repeatedly announce waiting "
        "or return empty output. If existing dependency replies block standby, inspect "
        "comms_inbox with this goal_id and wait_for. Read standby_review.messages and "
        "copy only standby_review.reviewed_inputs into reviewed_inputs; "
        "this records your decision to wait for a later reply without replaying uncertain inputs.",
        (
            ToolParameter("goal_id", "string", "Goal identity provided in the turn context"),
            ToolParameter(
                "status",
                "string",
                "Goal state",
                choices=("active", "standby", "completed", "blocked"),
            ),
            ToolParameter("progress", "string", "Progress summary or reason input is needed"),
            ToolParameter(
                "wait_for",
                "array",
                "Explicit thread names or @names; required for standby",
                required=False,
            ),
            ToolParameter(
                "reviewed_inputs",
                "array",
                "Exact unresolved inputId keys inspected and handled for this goal; standby only",
                required=False,
            ),
        ),
        _goal,
    ),
    ToolDeclaration(
        "comms_resume_goal",
        "Resume paused goal",
        "Resume your own paused goal with the same ID only after an explicit user request. "
        "This does not replace the goal or resume blocked/completed goals.",
        (
            ToolParameter("goal_id", "string", "Identity of the paused goal to resume"),
            ToolParameter("progress", "string", "Progress summary for the resumed goal"),
        ),
        _resume_goal,
    ),
    ToolDeclaration(
        "comms_edit_goal",
        "Edit goal text",
        "Edit your existing goal text while keeping its identity, status, and progress. "
        "Use comms_set_goal only to replace the objective with a fresh goal ID.",
        (
            ToolParameter("goal_id", "string", "Identity of the existing goal to edit"),
            ToolParameter("text", "string", "Revised objective text, including any @mentions"),
        ),
        _edit_goal,
    ),
    ToolDeclaration(
        "comms_goal_history",
        "Goal history",
        "Read recorded goal revisions and clearly labeled legacy baselines or observation gaps.",
        (
            ToolParameter(
                "goal_id",
                "string",
                "Optional goal ID; omit for this thread's full history",
                required=False,
                default="",
            ),
        ),
        _goal_history,
    ),
    ToolDeclaration(
        "comms_threads",
        "Comms Threads",
        "List agent threads on the coordination wire with status and pending counts.",
        (
            ToolParameter(
                "active_only", "boolean", "Exclude stopped threads", required=False, default=False
            ),
        ),
        _threads,
    ),
    ToolDeclaration(
        "comms_collaboration",
        "Manage mutual collaboration",
        "Create, update or end one mutual contact with another agent. Either participant "
        "can add, change its shared note, or remove it from both collaboration lists. "
        "This persistent metadata does not message, wake or fork either agent.",
        (
            ToolParameter(
                "action", "string", "Relationship change", choices=("add", "update", "remove")
            ),
            ToolParameter("peer", "string", "Collaborating agent thread name or alias"),
            ToolParameter(
                "note", "string", "Short description of ongoing work", required=False, default=""
            ),
        ),
        _collaboration,
    ),
    ToolDeclaration(
        "comms_collaborations",
        "List collaborations",
        "Read explicit contacts and goal-derived awareness with provenance, "
        "without changing delivery, work acceptance or owners.",
        (),
        _collaborations,
    ),
    ToolDeclaration(
        "comms_rename_self",
        "Rename Comms Thread",
        "Rename your own persistent thread identity; old names remain routing aliases.",
        (ToolParameter("new_name", "string", "Your new thread name"),),
        _rename_self,
    ),
    ToolDeclaration(
        "comms_send",
        "Comms Send",
        "Send a message to another thread or channel. Address specific threads with "
        "@thread-name in the body: the named threads are told they are the intended "
        "readers, and others dismiss quietly. Unknown names stay plain text.",
        (
            ToolParameter("from", "string", "Sender thread name"),
            ToolParameter("to", "string", "Target thread name or channel"),
            ToolParameter("body", "string", "Message text"),
            ToolParameter(
                "type",
                "string",
                "Message type",
                required=False,
                default="info",
                choices=tuple(message_type.value for message_type in MessageType),
            ),
        ),
        _send,
    ),
    ToolDeclaration(
        "comms_inbox",
        "Comms Inbox",
        "Fetch undelivered messages and unresolved native input attempts for a thread. "
        "Optional ACK changes only the inbox marker; unresolved_inputs remain UNKNOWN. "
        "Oversized results return complete=false, counts, and a complete result_file; "
        "ACK is deferred. Read that file selectively, never dump it into context. "
        "For standby, supply goal_id and wait_for to get standby_review: exact eligible keys "
        "and messages, already reviewed entries, and excluded owner/other-dependency inputs.",
        (
            ToolParameter("thread", "string", "Thread name"),
            ToolParameter(
                "ack", "boolean", "Mark delivered after reading", required=False, default=True
            ),
            ToolParameter(
                "goal_id", "string", "Current goal for scoped standby review", required=False
            ),
            ToolParameter(
                "wait_for", "array", "Declared dependencies for standby review", required=False
            ),
        ),
        _inbox,
    ),
    ToolDeclaration(
        "comms_fork",
        "Comms Fork",
        "Fork a child agent thread from a registered parent's persistent session.",
        (
            ToolParameter("name", "string", "Child thread name"),
            ToolParameter("parent", "string", "Parent thread name", context_value="subject"),
            ToolParameter("task", "string", "Task for the child"),
            ToolParameter("tags", "string", "Comma-separated tags", required=False, default=""),
            ToolParameter(
                "prompt", "string", "Initial prompt override", required=False, default=None
            ),
        ),
        _fork,
        context="thread",
        action_label="Fork from this thread",
        action_order=10,
    ),
    ToolDeclaration(
        "comms_stop",
        "Stop Comms Thread",
        "Stop a thread after verifying that its process owns the registered identity.",
        (ToolParameter("name", "string", "Thread name", context_value="subject"),),
        _stop,
        context="thread",
        action_label="Stop process",
        action_order=20,
    ),
    ToolDeclaration(
        "comms_start",
        "Start Comms Thread",
        "Start a stopped agent thread with its saved conversation and configuration. "
        "Reuses an already-running owner without interrupting it. The returned PID is "
        "reserved; startup may still be in progress. Does not send a new task.",
        (ToolParameter("name", "string", "Thread to start", context_value="subject"),),
        _start,
        context="thread",
        action_label="Start thread",
        action_order=21,
    ),
    ToolDeclaration(
        "comms_archive",
        "Archive Comms Thread",
        "Archive a stopped thread while retaining its messages.",
        (ToolParameter("name", "string", "Stopped thread name", context_value="subject"),),
        _archive,
        context="thread",
        action_label="Archive stopped thread",
        action_order=30,
    ),
    ToolDeclaration(
        "comms_delete",
        "Delete Comms Thread",
        "Delete a stopped thread and its owned wire state; detach and preserve its children.",
        (
            ToolParameter(
                "name",
                "string",
                "Stopped thread name",
                context_value="subject",
            ),
        ),
        _delete,
        context="thread",
        action_label="Delete stopped thread",
        action_order=40,
    ),
    ToolDeclaration(
        "comms_dismiss",
        "Dismiss Unaddressed Channel Pings",
        "End this turn quietly when a channel message is not addressed to you. "
        "Reports which threads were mentioned, advances only your own delivery "
        "cursor for that channel, and never wakes or notifies anyone else.",
        (
            ToolParameter(
                "thread",
                "string",
                "Thread dismissing its own pings (defaults to the executing thread)",
                required=False,
                default=None,
                context_value="actor",
            ),
            ToolParameter(
                "target",
                "string",
                "Channel the ping arrived on",
            ),
        ),
        _dismiss,
    ),
    ToolDeclaration(
        "comms_ack",
        "Acknowledge Comms Messages",
        "Mark a thread inbox or one of its conversations delivered.",
        (
            ToolParameter(
                "thread",
                "string",
                "Thread whose delivery cursor advances",
                context_value="actor",
            ),
            ToolParameter(
                "target",
                "string",
                "Optional conversation target",
                required=False,
                default=None,
                context_value="subject",
            ),
        ),
        _ack,
        context="thread",
        action_label="Mark inbox read",
        action_order=50,
    ),
)

_TOOLS_BY_NAME = {tool.name: tool for tool in TOOLS}


def tool_catalog() -> list[JsonObject]:
    """Return the adapter-neutral JSON Schema catalog."""
    return [tool.schema() for tool in TOOLS]


def context_tool_catalog(context: str) -> list[JsonObject]:
    """Return declared operations suitable for a resource context menu."""
    tools = sorted(
        (tool for tool in TOOLS if tool.context == context),
        key=lambda tool: tool.action_order,
    )
    return [tool.schema() for tool in tools]


def invoke_tool(comms: Comms, name: str, arguments: Mapping[str, object]) -> JsonObject:
    """Validate and invoke a declared tool against the shared operations layer."""
    try:
        declaration = _TOOLS_BY_NAME[name]
    except KeyError:
        raise ValueError(f"Unknown agent-comms tool {name!r}.") from None
    return declaration.invoke(comms, arguments)


def invoke_context_tool(
    comms: Comms,
    name: str,
    *,
    subject: str,
    actor: str | None = None,
    arguments: Mapping[str, object] | None = None,
) -> JsonObject:
    """Bind adapter-neutral context values, then invoke a declared operation."""
    try:
        declaration = _TOOLS_BY_NAME[name]
    except KeyError:
        raise ValueError(f"Unknown agent-comms tool {name!r}.") from None
    if declaration.context is None:
        raise ValueError(f"Tool {name!r} is not a context action.")
    bound: JsonObject = dict(arguments or {})
    for parameter in declaration.parameters:
        if parameter.context_value == "subject":
            bound[parameter.name] = subject
        elif parameter.context_value == "actor":
            if actor is None:
                raise ValueError(f"Tool {name!r} requires an acting thread.")
            bound[parameter.name] = actor
    return declaration.invoke(comms, bound)
