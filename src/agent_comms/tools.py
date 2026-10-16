"""Named external tool requests; declarations own schema, context and behavior."""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Mapping
from dataclasses import MISSING, asdict, dataclass, field, fields, replace
from typing import TYPE_CHECKING, ClassVar, Literal, Type

from .pi_vocabulary import ServiceTier, ThinkingLevel
from .channel_management import TagAction, TagDisposition, KeepThreadsTagDisposition
from .channels import SavedView, ViewKind, ViewMatch, ViewPredicate
from .declared_family import DeclaredFamily
from .display_order import ChannelSort, ThreadSort
from .errors import RelationViolationError
from .field_codec import FieldCodec
from .launch_runtime import LaunchRuntime
from .goal_actions import (
    ActiveGoalAction,
    EditGoalAction,
    GoalAction,
    GoalPrecondition,
    ModelInvocable,
    SetGoalAction,
)
from .goal_states import ActiveGoal
from .change_report import ChangeReport
from .messages import InfoKind, MessageKind
from .native_tools import NativeExecutableTool, NativeRequestTools
from .tool_caller import ToolCaller, ThreadCaller, NoThreadCaller
from .task_sources import (
    CurrentTaskScopeSelection, Constraint, Decision, Subtask, TaskChange,
    TaskScopeSelection, OriginalTaskChange,
)
from .thread_identity import TurnId
from .relationships import RelationshipEdit
from .thread_status import ThreadStatus
from .tool_output import (
    MAX_INLINE_OUTPUT_BYTES,
    materialize_oversized_output,
    serialize_tool_output,
)

if TYPE_CHECKING:
    from .comms import Comms

JsonObject = dict[str, object]


class ContextBinding(DeclaredFamily, affix="Binding"):
    @classmethod
    @abstractmethod
    def resolve(cls, subject: str, actor: str | None) -> str: ...


class SubjectBinding(ContextBinding):
    @classmethod
    def resolve(cls, subject: str, actor: str | None) -> str:
        return subject


def tool_field(
    description: str,
    *,
    default=MISSING,
    default_factory=MISSING,
    wire_name=None,
    binding: type[ContextBinding] | None = None,
    choices=None,
):
    metadata = {"description": description, "wire_nonnull": True}
    if wire_name is not None:
        metadata["wire_name"] = wire_name
    if binding is not None:
        metadata["context_binding"] = binding
    if choices is not None:
        metadata["wire_choices"] = choices
    return field(default=default, default_factory=default_factory, metadata=metadata)


@dataclass(frozen=True, kw_only=True)
class ToolRequest[Caller: ToolCaller = ToolCaller](NativeRequestTools, NativeExecutableTool):
    family_discriminator = "tool"
    description: ClassVar[str]
    context: ClassVar[str | None] = None
    action_label: ClassVar[str | None] = None
    action_order: ClassVar[int] = 0

    @classmethod
    def available_for(cls, status: ThreadStatus, *, owner_pid: int) -> bool:
        return True

    @classmethod
    def available_for_thread(cls, thread, status: ThreadStatus) -> bool:
        return cls.available_for(status, owner_pid=thread.pid)
    @classmethod
    def context_bindings(cls) -> dict[str, type[ContextBinding]]:
        return {
            declared.metadata.get("wire_name", declared.name): declared.metadata["context_binding"]
            for declared in fields(cls)
            if "context_binding" in declared.metadata
        }

    @classmethod
    def schema(cls) -> JsonObject:
        result = {
            "name": cls.declared_name,
            "label": cls.label,
            "description": cls.description,
            "parameters": FieldCodec.record_schema(cls),
        }
        if cls.context is not None:
            result.update(
                context=cls.context,
                action_label=cls.action_label or cls.label,
                action_order=cls.action_order,
                context_bindings={
                    key: binding.declared_name for key, binding in cls.context_bindings().items()
                },
            )
        return result

    @classmethod
    def invoke(cls, comms: Comms, arguments: Mapping[str, object], *, caller: ToolCaller = NoThreadCaller()) -> JsonObject:
        if cls.family_discriminator in arguments:
            raise ValueError("Unknown tool argument: tool")
        try:
            request = FieldCodec.decode(cls, {**arguments, cls.family_discriminator: cls.declared_name})
        except (TypeError, ValueError) as error:
            raise ValueError(f"Invalid {cls.declared_name} arguments: {error}") from error
        return FieldCodec.encode(caller.invoke(request, comms))

    @abstractmethod
    def apply(self, comms: Comms, caller: Caller) -> JsonObject: ...

    def apply_without_thread(self: ToolRequest[ToolCaller], comms: Comms, caller: NoThreadCaller) -> JsonObject:
        return self.apply(comms, caller)


class ThreadToolRequest(ToolRequest[ThreadCaller]):
    """Self-relative requests declare their required caller at the shared boundary."""

    def apply_without_thread(self, comms: Comms, caller: NoThreadCaller) -> JsonObject:
        return caller.refuse_thread()


def _tag_set(value: str) -> frozenset[str]:
    return frozenset(tag.strip() for tag in value.split(",") if tag.strip())


def _bounded_inbox_response(
    messages: list[JsonObject],
    unresolved: list[JsonObject],
    review: JsonObject | None,
    result_file: str,
    *,
    ack: bool,
) -> JsonObject:
    """Project a saved inbox snapshot without changing any delivery or goal authority."""
    bounded: JsonObject = {
        "messages": [],
        "acknowledged": 0,
        "unresolved_inputs": [],
        "complete": False,
        "ackDeferred": ack,
        "counts": {"messages": len(messages), "unresolved_inputs": len(unresolved)},
        "result_file": result_file,
        "instruction": (
            "The messages and unresolved_inputs arrays are omitted, not empty. The file is "
            "a complete public result snapshot, not current authority. Read it selectively "
            "with read offset/limit or a JSON query; do not dump the whole file into "
            "context. No messages were acknowledged. UNKNOWN inputs are unchanged. When "
            "standby_review.messages is present, its complete eligible messages and exact "
            "reviewed_inputs are inline; other review arrays are omitted with counts. "
            "Otherwise read the review from the file. Inspect the full relevant messages "
            "before passing their reviewed_inputs to comms_goal."
        ),
    }
    if review is not None:
        review_summary: JsonObject = {
            "complete": False,
            "counts": {
                key: len(review[key])
                for key in ("messages", "already_reviewed_inputs", "excluded_inputs")
            },
        }
        bounded["standby_review"] = review_summary
        candidate = {
            **bounded,
            "standby_review": {
                **review_summary,
                "goal_id": review["goal_id"],
                "wait_for": review["wait_for"],
                "messages": review["messages"],
                "reviewed_inputs": review["reviewed_inputs"],
                "messages_complete": True,
            },
        }
        if len(serialize_tool_output(candidate).encode("utf-8")) <= MAX_INLINE_OUTPUT_BYTES:
            return candidate
    return bounded


class NativeOwnerCommand:
    @classmethod
    def available_for_thread(cls, thread, status: ThreadStatus) -> bool:
        return thread.execution.native_command_available(cls, status, owner_pid=thread.pid)


@dataclass(frozen=True, kw_only=True)
class CommsPinChannelTool(ToolRequest):
    label = "Pin Channel"
    description = (
        "Persist whether a channel is pinned. Pinned channels sort before unpinned "
        "channels; the selected channel sort remains the secondary order."
    )
    name: str = tool_field("Channel name")
    pinned: bool = tool_field("True to pin; false to unpin")

    def apply(self, comms: Comms, caller: ToolCaller) -> JsonObject:
        from .cli_commands import PinChannelCliCommand

        return PinChannelCliCommand(name=self.name, pinned=self.pinned).apply(comms)


@dataclass(frozen=True, kw_only=True)
class CommsPinThreadTool(ToolRequest):
    label = "Pin Thread in Channel"
    description = (
        "Persist whether a member is pinned within one channel. Pins do not change "
        "membership or ordering in other channels. The selected member sort remains the"
        " secondary order."
    )
    channel: str = tool_field("Channel containing the thread")
    name: str = tool_field("Member thread name")
    pinned: bool = tool_field("True to pin; false to unpin")

    def apply(self, comms: Comms, caller: ToolCaller) -> JsonObject:
        from .cli_commands import PinThreadCliCommand

        channel = self.channel
        PinThreadCliCommand(channel=channel, name=self.name, pinned=self.pinned).apply(comms)
        canonical = channel if channel.startswith("#") else f"#{channel}"
        return next(
            view.to_wire() for view in comms.views.channel_views() if view.channel.name == canonical
        )


@dataclass(frozen=True, kw_only=True)
class CommsModelTool(NativeOwnerCommand, ToolRequest):
    label = "Change Thread Model"
    description = (
        "Change this thread's model, or another thread's model, for its future turns. "
        "Optionally change its thinking level and provider service tier too. Applies "
        "from that thread's next turn."
    )
    thread: str | None = tool_field(
        "Thread to change (defaults to the executing thread)", default=None, binding=SubjectBinding
    )
    model: str = tool_field("Provider/model identifier")
    thinking_level: str = tool_field(
        "Optional Pi thinking level", default="", choices=ThinkingLevel.names
    )
    service_tier: ServiceTier | None = tool_field(
        "Optional provider request service tier", default=None
    )

    def apply(self, comms: Comms, caller: ToolCaller) -> JsonObject:
        """Change this thread's model or another thread's model for future turns."""
        thread_name = caller.subject(self.thread or "")
        original = comms.registry.require(thread_name)
        if not caller.may_reconfigure(comms, thread_name):
            raise RelationViolationError(
                f"{thread_name} is running; change its settings through its own session"
            )
        model = self.model.strip()
        thinking_level = (self.thinking_level or "").strip()
        thread = comms.threads.set_thread_configuration(
            original,
            model=model or None,
            thinking_level=thinking_level or None,
            service_tier=self.service_tier,
        )
        return {
            "thread": thread.name,
            "model": thread.model,
            "thinking_level": ThinkingLevel.optional_name(thread.thinking_level),
            "service_tier": thread.service_tier.value,
        }


@dataclass(frozen=True, kw_only=True)
class CommsSortChannelsTool(ToolRequest):
    label = "Sort Channels"
    description = (
        "Persist channel-list ordering independently of member ordering. Pinned "
        "channels stay first, with built-in channels first within each pin group."
    )
    order: type[ChannelSort] = tool_field("Channel list order")

    def apply(self, comms: Comms, caller: ToolCaller) -> JsonObject:
        order = comms.channels.set_channel_order(self.order)
        return {"order": FieldCodec.encode(order)}


@dataclass(frozen=True, kw_only=True)
class CommsTagsTool(ToolRequest):
    label = "Manage tags"
    description = (
        "List, create, rename, or delete tags. Renaming/deleting updates thread "
        "assignments and channel filters. Uncovered tags automatically have a channel."
    )
    action: TagAction = tool_field("Operation", default=TagAction.LIST)
    name: str = tool_field("Tag name", default="")
    new_name: str = tool_field("Replacement tag for rename", default="")
    disposition: TagDisposition = tool_field("Tagged-thread operation", default_factory=KeepThreadsTagDisposition)
    confirmed: bool = tool_field("Confirm thread archiving or deletion", default=False)

    def apply(self, comms: Comms, caller: ToolCaller) -> JsonObject:
        tags = self.action.apply(comms.channels, self.name, self.new_name,
                                disposition=self.disposition, confirmed=self.confirmed)
        return {"tags": sorted(tags)}


@dataclass(frozen=True, kw_only=True)
class CommsThreadTagsTool(ToolRequest):
    label = "Assign thread tags"
    description = (
        "Add/remove tags on a thread. Omit thread to manage your own tags. Channel "
        "membership and routing update immediately without restarting or creating an "
        "executor."
    )
    thread: str = tool_field("Thread name or alias", default="")
    add: str = tool_field("Comma-separated tags to add", default="")
    remove: str = tool_field("Comma-separated tags to remove", default="")

    def apply(self, comms: Comms, caller: ToolCaller) -> JsonObject:
        thread = comms.channels.update_tags(
            caller.subject(self.thread),
            add=_tag_set(self.add),
            remove=_tag_set(self.remove),
        )
        return {"thread": thread.name, "tags": sorted(thread.tags)}


@dataclass(frozen=True, kw_only=True)
class CommsChannelsTool(ToolRequest):
    label = "List channels"
    description = "List named tag views and their current member threads; #any includes everyone."

    def apply(self, comms: Comms, caller: ToolCaller) -> JsonObject:
        return {
            "channels": [view.to_wire() for view in comms.views.channel_views()],
            "views": [
                FieldCodec.encode(view)
                for view in comms.channels.catalog.read().saved_views.values()
            ],
            "order": FieldCodec.encode(comms.channels.catalog.read().list_order),
        }


@dataclass(frozen=True, kw_only=True)
class CommsSetChannelMetadataTool(ToolRequest):
    label = "Set channel presentation"
    description = (
        "Set presentation-only parent and archive state. These fields never alter "
        "routing, membership, history, unread state, or delivery."
    )
    name: str = tool_field("Exact channel name")
    parent: str = tool_field("Parent channel; empty clears the parent")
    archived: bool = tool_field("Whether to hide from active presentation")

    def apply(self, comms: Comms, caller: ToolCaller) -> JsonObject:
        parent = self.parent.strip() or None
        return comms.channels.set_channel_metadata(
            self.name, parent=parent, archived=self.archived
        ).to_wire()


@dataclass(frozen=True, kw_only=True)
class CommsSetViewTool(ToolRequest):
    label = "Set non-routable view"
    description = (
        "Create/update a typed participant or activity projection. Views cannot receive"
        " messages and never own channel history."
    )
    name: str = tool_field("View name without #")
    kind: ViewKind = tool_field("Projection kind")
    match: type[ViewMatch] = tool_field("Tag predicate")
    tags: str = tool_field("One or more comma-separated tags")

    def apply(self, comms: Comms, caller: ToolCaller) -> JsonObject:
        return FieldCodec.encode(
            comms.channels.set_saved_view(
                SavedView(self.name, self.kind, ViewPredicate(self.match, _tag_set(self.tags)))
            )
        )


@dataclass(frozen=True, kw_only=True)
class CommsDeleteViewTool(ToolRequest):
    label = "Delete non-routable view"
    description = "Delete a saved projection without changing tags, channels, messages, or threads."
    name: str = tool_field("Saved view name")

    def apply(self, comms: Comms, caller: ToolCaller) -> JsonObject:
        from .cli_commands import DeleteViewCliCommand

        DeleteViewCliCommand(name=self.name).apply(comms)
        return CommsChannelsTool().apply(comms, caller)


@dataclass(frozen=True, kw_only=True)
class CommsSortChannelTool(ToolRequest):
    label = "Sort channel members"
    description = "Persist one channel's member ordering, shared by all attached views."
    name: str = tool_field("Channel name")
    order: type[ThreadSort] = tool_field("Member ordering")

    def apply(self, comms: Comms, caller: ToolCaller) -> JsonObject:
        return comms.channels.set_channel_sort(self.name, self.order).to_wire()


@dataclass(frozen=True, kw_only=True)
class CommsSetProjectTool(ThreadToolRequest):
    label = "Change Project"
    description = (
        "Change your own persistent project directory and synchronize attached clients."
        " Accepts an existing absolute path, ~ path, or path relative to your current "
        "project. Preserves this thread and its conversation. After changing, end your "
        "turn; the runtime automatically continues in the new project."
    )
    path: str = tool_field("New project directory")

    def apply(self, comms: Comms, caller: ThreadCaller) -> JsonObject:
        result = comms.threads.set_project(caller.name, self.path)
        return {
            **asdict(result),
            "instruction": (
                (
                    "Project saved. End this turn now; the runtime automatically resumes this same "
                    "conversation in the new project with refreshed tools and context."
                )
                if result.changed
                else "This is already your project directory."
            ),
        }


@dataclass(frozen=True, kw_only=True)
class CommsSetGoalTool(ThreadToolRequest):
    label = "Set persistent goal"
    description = (
        "Set or replace your own persistent goal. Use this when the user asks you to "
        "start, set, or pursue a goal autonomously."
    )
    text: str = tool_field("Persistent objective to pursue")

    def apply(self, comms: Comms, caller: ThreadCaller) -> JsonObject:
        goal = comms.goals.update_goal(caller.name, SetGoalAction(text=self.text))
        return {
            "goal": goal.to_wire() if goal else None,
            "instruction": (
                "Persistent goal activated. Work toward it and report progress with comms_goal."
            ),
        }


@dataclass(frozen=True, kw_only=True)
class CommsGoalTool(ThreadToolRequest):
    label = "Goal progress"
    description = (
        "Update goal progress; complete it or block it when user input is needed. Use "
        "standby with explicit wait_for thread names when waiting for delegated work. "
        "The goal remains active, but only a direct message from a named dependency or "
        "a user follow-up starts its next turn. Do not repeatedly announce waiting or "
        "return empty output. If existing dependency replies block standby, inspect "
        "comms_inbox with this goal_id and wait_for. Read standby_review.messages and "
        "copy only standby_review.reviewed_inputs into reviewed_inputs; this records "
        "your decision to wait for a later reply without replaying uncertain inputs."
    )
    goal_id: str = tool_field("Goal identity provided in the turn context")
    status: type[GoalAction] = tool_field("Goal state", choices=GoalAction.model_choices)
    progress: str = tool_field("Progress summary; blocked requires a nonempty explicit reason")
    wait_for: list[str] | None = tool_field(
        "Explicit thread names or @names; required for standby", default=None
    )
    reviewed_inputs: list[str] | None = tool_field(
        "Exact unresolved inputId keys inspected and handled for this goal; standby only",
        default=None,
    )

    def apply(self, comms: Comms, caller: ThreadCaller) -> JsonObject:
        name = caller.name
        command = self.status(
            progress=self.progress,
            **{
                key: value
                for key, value in (
                    ("wait_for", self.wait_for),
                    ("reviewed_inputs", self.reviewed_inputs),
                )
                if value
            },
        )
        command = replace(
            command,
            expect=GoalPrecondition(goal_id=self.goal_id, expected_state=ActiveGoal()),
        )
        comms.goals.update_goal(name, command, actor=ModelInvocable)
        goal, execution = comms.goals.goal_snapshot(name)
        return {
            "goal": goal.to_wire() if goal else None,
            "goal_execution": execution.to_wire() if execution else None,
        }


@dataclass(frozen=True, kw_only=True)
class CommsResumeGoalTool(ThreadToolRequest):
    label = "Resume paused goal"
    description = (
        "Resume your own paused goal with the same ID only after an explicit user "
        "request. This does not replace the goal or resume blocked/completed goals."
    )
    goal_id: str = tool_field("Identity of the paused goal to resume")
    progress: str = tool_field("Progress summary for the resumed goal")

    def apply(self, comms: Comms, caller: ThreadCaller) -> JsonObject:
        name = caller.name
        goal_id = self.goal_id
        current = comms.registry.require(name).goal_for(goal_id)
        if current is None:
            raise ValueError("This goal cannot be resumed; refresh its state.")
        current.state.require_model_resume()
        progress = self.progress
        goal = comms.goals.update_goal(
            name,
            ActiveGoalAction(
                expect=GoalPrecondition(
                    expected_goal=current,
                    goal_id=goal_id,
                ),
                progress=progress,
            ),
        )
        if goal is None or not goal.state.active or goal.progress != progress:
            raise ValueError("Goal changed during resume; refresh its state.")
        return {"goal": goal.to_wire()}


@dataclass(frozen=True, kw_only=True)
class CommsEditGoalTool(ThreadToolRequest):
    label = "Edit goal text"
    description = (
        "Edit your existing goal text while keeping its identity, status, and progress."
        " Use comms_set_goal only to replace the objective with a fresh goal ID."
    )
    goal_id: str = tool_field("Identity of the existing goal to edit")
    text: str = tool_field("Revised objective text, including any @mentions")

    def apply(self, comms: Comms, caller: ThreadCaller) -> JsonObject:
        name = caller.name
        goal_id = self.goal_id
        current = comms.registry.require(name).require_goal(goal_id)
        goal = comms.goals.update_goal(
            name,
            EditGoalAction(
                expect=GoalPrecondition(expected_goal=current, goal_id=goal_id), text=self.text
            ),
        )
        return {"goal": goal.to_wire() if goal else None}


@dataclass(frozen=True, kw_only=True)
class CommsGoalHistoryTool(ThreadToolRequest):
    label = "Goal history"
    description = "Read recorded goal revisions and recorded provenance and observation gaps."
    goal_id: str = tool_field("Optional goal ID; omit for this thread's full history", default="")

    def apply(self, comms: Comms, caller: ThreadCaller) -> JsonObject:
        requested = self.goal_id.strip()
        entries = comms.goals.goal_history(caller.name, goal_id=requested or None)
        return {"history": [entry.to_wire() for entry in entries]}


@dataclass(frozen=True, kw_only=True)
class CommsThreadsTool(ToolRequest):
    label = "Comms Threads"
    description = (
        "List agent threads with status and pending counts, plus this caller's "
        "recent sent-message delivery outcomes."
    )
    active_only: bool = tool_field("Exclude stopped threads", default=False)

    def apply(self, comms: Comms, caller: ToolCaller) -> JsonObject:
        from .cli_commands import ThreadsCliCommand

        return caller.listing(comms, ThreadsCliCommand(active_only=self.active_only).apply(comms))


@dataclass(frozen=True, kw_only=True)
class CommsCollaborationTool(ThreadToolRequest):
    label = "Manage mutual collaboration"
    description = (
        "Create, update or end one mutual contact with another agent. Either "
        "participant can add, change its shared note, or remove it from both "
        "collaboration lists. This persistent metadata does not message, wake or fork "
        "either agent."
    )
    action: type[RelationshipEdit] = tool_field("Relationship change")
    peer: str = tool_field("Collaborating agent thread name or alias")
    note: str = tool_field("Short description of ongoing work", default="")

    def apply(self, comms: Comms, caller: ThreadCaller) -> JsonObject:
        relationship = comms.relationships.edit(
            caller.name, self.action, self.peer, self.note
        )
        return {"collaboration": asdict(relationship) if relationship else None}


@dataclass(frozen=True, kw_only=True)
class CommsCollaborationsTool(ThreadToolRequest):
    label = "List collaborations"
    description = (
        "Read explicit contacts and goal-derived awareness with provenance, without "
        "changing delivery, work acceptance or owners."
    )

    def apply(self, comms: Comms, caller: ThreadCaller) -> JsonObject:
        owner = caller.name
        contacts = comms.relationships.contacts(owner)
        result: JsonObject = {"collaborations": [asdict(edge) for edge in contacts.explicit]}
        if any(entry.goal_contacts for entry in contacts.visible):
            result["visible_collaborators"] = [
                {
                    "peer": entry.target,
                    "available": entry.available,
                    "sources": entry.sources,
                    "detail": entry.detail,
                    "goal_contacts": [asdict(contact) for contact in entry.goal_contacts],
                }
                for entry in contacts.visible
            ]
        if contacts.diagnostics:
            result["unresolved_goal_mentions"] = [asdict(row) for row in contacts.diagnostics]
        return result


@dataclass(frozen=True, kw_only=True)
class CommsRenameSelfTool(ThreadToolRequest):
    label = "Rename Comms Thread"
    description = "Rename your own persistent thread identity; old names remain routing aliases."
    new_name: str = tool_field("Your new thread name")

    def apply(self, comms: Comms, caller: ThreadCaller) -> JsonObject:
        return asdict(comms.threads.rename_self(self.new_name, caller=caller))


@dataclass(frozen=True, kw_only=True)
class CommsSendTool(ToolRequest):
    label = "Comms Send"
    description = (
        "Send a message to another thread or channel. Address specific threads with "
        "@thread-name in the body: the named threads are told they are the intended "
        "readers, and others dismiss quietly. Unknown names stay plain text."
    )
    sender: str = tool_field("Sender thread name", wire_name="from")
    target: str = tool_field("Target thread name or channel", wire_name="to")
    body: str = tool_field("Message text")
    type: Type[MessageKind] = tool_field(
        "Message kind", default=InfoKind, choices=MessageKind.sendable_names
    )
    report: ChangeReport | None = tool_field(
        "Change report fields; required for the change-report kind", default=None
    )

    def apply(self, comms: Comms, caller: ToolCaller) -> JsonObject:
        message = comms.messaging.send_message(
            self.sender, self.target, self.body, self.type, report=self.report
        )
        from .pi_payloads import PublishedMessageToolDetails
        from .transcript_events import SentTranscript

        return PublishedMessageToolDetails(
            id=message.message_id, message=SentTranscript.from_message(message)).to_wire()


@dataclass(frozen=True, kw_only=True)
class CommsAuthoredTaskTool(ThreadToolRequest):
    """One original admitted publication path for authored task declarations."""
    target: str = tool_field("Thread or channel receiving the original declaration", wire_name="to")
    scope: TaskScopeSelection = tool_field(
        "Current project/goal or explicit scope", default=CurrentTaskScopeSelection())
    change: TaskChange = tool_field(
        "Original declaration or correction naming its original reference",
        default=OriginalTaskChange())

    @abstractmethod
    def declaration(self, owner): ...

    @abstractmethod
    def original_body(self, declaration): ...

    def apply(self, comms: Comms, caller: ThreadCaller) -> JsonObject:
        owner = comms.registry.require(caller.name)
        declaration = self.declaration(owner)
        message = comms.messaging.send_message(
            owner.name, self.target, self.original_body(declaration),
            notice=True, task=declaration)
        return {"reference": FieldCodec.encode(message.reference),
                "task": FieldCodec.encode(message.task)}


@dataclass(frozen=True, kw_only=True)
class CommsDecisionTool(CommsAuthoredTaskTool):
    label = "Record Decision"
    description = (
        "Record a chosen alternative and valid rejected alternatives on its original "
        "wire message. Author and source turn come from your admitted execution. "
        "Defaults to current project/goal revision or current turn; corrections name "
        "the original reference and grant no execution.")
    chosen: str = tool_field("Chosen alternative, preserving exact wording")
    rejected: tuple[str, ...] = tool_field("Nonempty unique valid rejected alternatives")

    def declaration(self, owner):
        return Decision.from_admission(owner, self.scope, self.change,
                                       chosen=self.chosen, rejected=self.rejected)

    def original_body(self, declaration):
        return declaration.text


@dataclass(frozen=True, kw_only=True)
class CommsConstraintTool(CommsAuthoredTaskTool):
    label = "Record Constraint"
    description = (
        "Declare an exact authored restriction on its original wire message; wording "
        "is retained verbatim. This records your admitted authorship, not inferred "
        "human authority. Defaults to current project/goal revision or current turn. "
        "Corrections name the original constraint reference; no execution or replay "
        "is authorized.")
    text: str = tool_field("Exact authored restriction, never a paraphrase of another source")

    def declaration(self, owner):
        return Constraint.from_admission(owner, self.scope, self.change)

    def original_body(self, declaration):
        if not self.text.strip():
            raise ValueError("An authored constraint requires exact nonempty wording")
        return self.text


@dataclass(frozen=True, kw_only=True)
class CommsSubtaskTool(CommsAuthoredTaskTool):
    label = "Record Subtask"
    description = (
        "Explicitly record whether a described subtask is completed or unfinished. "
        "This is not assistant-turn termination or goal completion. Original author, "
        "turn, scope and correction reference use the existing admitted task owner. "
        "A completed observation may permit optional compaction when enabled.")
    text: str = tool_field("Exact description of the observed subtask")
    completed: bool = tool_field("Your explicit observation: completed or unfinished")

    def declaration(self, owner):
        if not self.text.strip():
            raise ValueError("A subtask observation requires its exact description")
        return Subtask.from_admission(owner, self.scope, self.change, completed=self.completed)

    def original_body(self, declaration):
        return self.text


@dataclass(frozen=True, kw_only=True)
class CommsInboxTool(ToolRequest):
    label = "Comms Inbox"
    description = (
        "Fetch undelivered messages and unresolved native input attempts for a thread. "
        "Optional target limits the conversation. ACK marks exactly the returned "
        "message references; unresolved_inputs remain UNKNOWN. Oversized results return "
        "complete=false, counts, and a complete result_file; ACK is deferred. Read that "
        "file selectively, never dump it into context. For standby, supply goal_id and "
        "wait_for to get standby_review: exact eligible keys and messages, already "
        "reviewed entries, and excluded owner/other-dependency inputs."
    )
    thread: str = tool_field("Thread name")
    target: str | None = tool_field("Optional channel or peer conversation", default=None)
    ack: bool = tool_field("Mark exactly the returned messages delivered", default=True)
    goal_id: str | None = tool_field("Current goal for scoped standby review", default=None)
    wait_for: list[str] | None = tool_field(
        "Declared dependencies for standby review", default=None
    )

    def apply(self, comms: Comms, caller: ToolCaller) -> JsonObject:
        thread = self.thread
        target = self.target
        if target is not None:
            target = target.strip()
            if not target:
                raise ValueError("An inbox target cannot be empty.")
        goal_id = self.goal_id
        wait_for = self.wait_for
        if bool(goal_id) != bool(wait_for):
            raise ValueError("Provide goal_id and wait_for together for standby review.")
        review = (
            comms.goals.goal_input_review(thread, goal_id, wait_for)
            if goal_id and wait_for
            else None
        )
        inbox = comms.bus.inbox(thread, target)
        messages = [message.to_wire() for message in inbox]
        references = tuple(message.reference for message in inbox)
        unresolved = comms.goals.unresolved_inputs(thread)
        response: JsonObject = {
            "messages": messages,
            "acknowledged": 0,
            "unresolved_inputs": unresolved,
            **({"standby_review": review} if review is not None else {}),
        }
        result_file = materialize_oversized_output(
            comms.root, response, inline_limit=MAX_INLINE_OUTPUT_BYTES - 64
        )
        if result_file is not None:
            return _bounded_inbox_response(
                messages, unresolved, review, str(result_file), ack=self.ack
            )
        response["acknowledged"] = (
            comms.messaging.acknowledge(thread, references) if self.ack else 0
        )
        return response


@dataclass(frozen=True, kw_only=True)
class CommsForkTool(NativeOwnerCommand, ToolRequest):
    label = "Comms Fork"
    description = "Fork a child agent thread from a registered parent's persistent session."
    context = "thread"
    action_label = "Fork from this thread"
    action_order = 10
    name: str = tool_field("Child thread name")
    parent: str = tool_field("Parent thread name", binding=SubjectBinding)
    task: str = tool_field("Optional task for the child", default="")
    tags: str | None = tool_field(
        "Comma-separated tags; omitted inherits parent tags", default=None
    )
    prompt: str | None = tool_field("Initial prompt override", default=None)

    def apply(self, comms: Comms, caller: ToolCaller) -> JsonObject:
        from .cli_commands import ForkCliCommand

        parent = comms.registry.require(self.parent)
        return ForkCliCommand(name=self.name, parent=parent.name, task=self.task,
                              project=parent.worktree, model=parent.model,
                              thinking_level=parent.thinking_level,
                              tags=_tag_set(self.tags) if self.tags is not None else None,
                              prompt=self.prompt).apply(comms)


class OwnerLifecycleControl:
    """Commands that act on the owner lifecycle share its eligibility query."""

    @classmethod
    def available_for(cls, status: ThreadStatus, *, owner_pid: int) -> bool:
        return status.allows_owner_control()


@dataclass(frozen=True, kw_only=True)
class CommsStopTool(NativeOwnerCommand, OwnerLifecycleControl, ToolRequest):
    label = "Stop Comms Thread"
    description = "Stop a thread after verifying that its process owns the registered identity."
    context = "thread"
    action_label = "Stop process"
    action_order = 20
    name: str = tool_field("Thread name", binding=SubjectBinding)

    def apply(self, comms: Comms, caller: ToolCaller) -> JsonObject:
        from .cli_commands import StopCliCommand

        return StopCliCommand(name=self.name).apply(comms)


@dataclass(frozen=True, kw_only=True)
class CommsStartTool(NativeOwnerCommand, OwnerLifecycleControl, ToolRequest):
    label = "Start Comms Thread"
    description = (
        "Start a stopped agent thread with its saved conversation and configuration. "
        "Reuses an already-running owner without interrupting it. The returned PID is "
        "reserved; startup may still be in progress. Does not send a new task."
    )
    context = "thread"
    action_label = "Start thread"
    action_order = 21
    name: str = tool_field("Thread to start", binding=SubjectBinding)

    @classmethod
    def available_for(cls, status: ThreadStatus, *, owner_pid: int) -> bool:
        return status.allows_owner_start(owner_pid=owner_pid)

    def apply(self, comms: Comms, caller: ToolCaller) -> JsonObject:
        from .cli_commands import StartCliCommand

        return StartCliCommand(name=self.name).apply(comms)


@dataclass(frozen=True, kw_only=True)
class CommsQueueRestartTool(NativeOwnerCommand, OwnerLifecycleControl, ToolRequest):
    label = "Queue Idle Owner Restart"
    description = (
        "Queue an exact live owner incarnation for restart when idle. Never interrupt an active "
        "turn. A changed owner or uncertain attempt is never retried automatically. "
        "Restarts in the current installed runtime; does not deploy another runtime."
    )
    context = "thread"
    action_label = "Queue idle restart"
    action_order = 22
    name: str = tool_field("Live agent thread", binding=SubjectBinding)

    def apply(self, comms: Comms, caller: ToolCaller) -> JsonObject:
        from .restart_queue import enqueue as enqueue_restart

        return {"restart": FieldCodec.encode(enqueue_restart(
            comms, self.name, launch_runtime=LaunchRuntime.current()))}


@dataclass(frozen=True, kw_only=True)
class CommsRestartQueueTool(ToolRequest):
    label = "Restart Queue Status"
    description = "Inspect queued, restarted, stale, or uncertain attempts for a thread."
    name: str = tool_field("Agent thread")

    def apply(self, comms: Comms, caller: ToolCaller) -> JsonObject:
        from .restart_queue import status as restart_status

        return {
            "restarts": [FieldCodec.encode(record) for record in restart_status(comms, self.name)]
        }


@dataclass(frozen=True, kw_only=True)
class CommsCancelRestartTool(ToolRequest):
    label = "Cancel Queued Restart"
    description = "Cancel pending restarts for a thread; cannot undo an attempted restart."
    name: str = tool_field("Agent thread")

    def apply(self, comms: Comms, caller: ToolCaller) -> JsonObject:
        from .restart_queue import cancel as cancel_restart

        return {
            "cancelled": [FieldCodec.encode(record) for record in cancel_restart(comms, self.name)]
        }


@dataclass(frozen=True, kw_only=True)
class CommsArchiveTool(OwnerLifecycleControl, ToolRequest):
    label = "Archive Comms Thread"
    description = "Archive a stopped thread while retaining its messages."
    context = "thread"
    action_label = "Archive stopped thread"
    action_order = 30
    name: str = tool_field("Stopped thread name", binding=SubjectBinding)

    @classmethod
    def available_for(cls, status: ThreadStatus, *, owner_pid: int) -> bool:
        return status.stopped

    def apply(self, comms: Comms, caller: ToolCaller) -> JsonObject:
        from .cli_commands import ArchiveCliCommand

        return ArchiveCliCommand(name=self.name).apply(comms)


def tool_catalog() -> list[JsonObject]:
    return [tool.schema() for tool in ToolRequest.members_with(ToolRequest)]


def context_tool_catalog(context: str) -> list[JsonObject]:
    tools = sorted(
        (tool for tool in ToolRequest.members_with(ToolRequest) if tool.context == context),
        key=lambda tool: tool.action_order,
    )
    return [tool.schema() for tool in tools]


def invoke_tool(comms: Comms, name: str, arguments: Mapping[str, object], *, caller: ToolCaller = NoThreadCaller()) -> JsonObject:
    return ToolRequest.decode(name).invoke(comms, arguments, caller=caller)


def invoke_context_tool(
    comms: Comms,
    name: str,
    *,
    subject: str,
    actor: str | None = None,
    arguments: Mapping[str, object] | None = None,
) -> JsonObject:
    tool = ToolRequest.decode(name)
    if tool.context is None:
        raise ValueError(f"Tool {name!r} is not a context action.")
    bound = dict(arguments or {})
    bound.update(
        (key, binding.resolve(subject, actor)) for key, binding in tool.context_bindings().items()
    )
    return tool.invoke(comms, bound, caller=ToolCaller.from_actor(actor))
