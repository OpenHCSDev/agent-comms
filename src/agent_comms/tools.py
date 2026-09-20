"""Declarative tool surface shared by process and protocol adapters."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal

from .declarations import MessageType
from .operations import Comms, ForkSpec

JsonObject = dict[str, object]
ToolHandler = Callable[[Comms, Mapping[str, object]], JsonObject]


@dataclass(frozen=True, slots=True)
class ToolParameter:
    name: str
    kind: Literal["string", "boolean"]
    description: str
    required: bool = True
    default: object | None = None
    choices: tuple[str, ...] = ()
    context_value: Literal["subject", "actor"] | None = None

    def schema(self) -> JsonObject:
        schema: JsonObject = {"type": self.kind, "description": self.description}
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
            expected = str if parameter.kind == "string" else bool
            if not isinstance(value, expected):
                raise ValueError(f"Argument {parameter.name!r} must be {parameter.kind}.")
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


def _send(comms: Comms, arguments: Mapping[str, object]) -> JsonObject:
    message_id = comms.send(
        str(arguments["from"]),
        str(arguments["to"]),
        str(arguments["body"]),
        MessageType(str(arguments["type"])),
    )
    return {"id": message_id}


def _inbox(comms: Comms, arguments: Mapping[str, object]) -> JsonObject:
    thread = str(arguments["thread"])
    messages = [message.to_wire() for message in comms.inbox(thread)]
    acknowledged = comms.acknowledge(thread) if arguments["ack"] else 0
    return {"messages": messages, "acknowledged": acknowledged}


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


def _ack(comms: Comms, arguments: Mapping[str, object]) -> JsonObject:
    target = arguments["target"]
    acknowledged = comms.acknowledge(
        str(arguments["thread"]), str(target) if target is not None else None
    )
    return {"acknowledged": acknowledged}


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
    }


TOOLS = (
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
        "comms_rename_self",
        "Rename Comms Thread",
        "Rename your own persistent thread identity; old names remain routing aliases.",
        (ToolParameter("new_name", "string", "Your new thread name"),),
        _rename_self,
    ),
    ToolDeclaration(
        "comms_send",
        "Comms Send",
        "Send a message to another thread or channel.",
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
        "Fetch undelivered messages for a thread and optionally mark them delivered.",
        (
            ToolParameter("thread", "string", "Thread name"),
            ToolParameter(
                "ack", "boolean", "Mark delivered after reading", required=False, default=True
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
        "Permanently delete a stopped, child-free thread and its owned wire state.",
        (
            ToolParameter(
                "name",
                "string",
                "Stopped, child-free thread name",
                context_value="subject",
            ),
        ),
        _delete,
        context="thread",
        action_label="Delete stopped thread",
        action_order=40,
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
