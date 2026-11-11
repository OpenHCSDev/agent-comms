"""The task-attachment contract and its absent member.

TaskAttachment and NoTaskAttachment live here, below both messages.py and
task_sources.py, so neither imports the other at module level for these two
names.  Concrete attachments stay in task_sources.py and agent_comms.task_attachments.

References to Message, RetainedTaskFacts, InputAttempt and StoredInput are
annotation-only (strings under ``from __future__ import annotations``).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from agent_comms.cli_bridges.harness_delivery_states import Outcome
from agent_comms.errors import RelationViolationError
from agent_comms.execution.delivers_input import DeliversInput
from agent_comms.execution.input_lookup import InputLookup
from agent_comms.execution.turn_context import ContextSegment, Provenance, WireProvenance
from agent_comms.messaging.channel_targets import CommandTarget
from agent_comms.messaging.message_directions import MessageDirections
from agent_comms.messaging.message_reads import MessageReferencesRead
from agent_comms.messaging.message_reference import MessageReference
from agent_comms.messaging.projection_source import ProjectionSource
from agent_comms.messaging.task_callbacks import DeliveredContext, TeamObservation
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import FieldCodec, ThreadLookup
from agent_comms.registry.task_owner import TaskOwner
from agent_comms.registry.task_registry import TaskRegistry
from agent_comms.storage.typed_table import TypedTable
from agent_comms.ui_model.message_category import MessageCategory


class TaskAttachment(
    DeclaredFamily, ProjectionSource, affix="TaskAttachment",
    discovery_package="agent_comms.task_attachments",
):
    """Absent attachments have no publication, fact or author effects.

    Members declared outside this module live in ``agent_comms.task_attachments``, which
    the registry acquires before any name is decoded.
    """

    permits_agent_revision = False
    observes_subtask = False

    def require_subtask(self) -> Subtask:
        raise RelationViolationError("Original wire message has no subtask observation")

    def require_scoped_task(self) -> ScopedTaskDeclaration:
        raise RelationViolationError("Original wire message has no declared scoped task")

    def require_constraint(self) -> Constraint:
        raise RelationViolationError("Original wire message has no declared constraint")

    def require_decision(self) -> Decision:
        raise RelationViolationError("Original wire message has no declared decision")

    def require_user_supersession(self) -> UserTaskSupersession:
        raise RelationViolationError("Original wire message has no declared user supersession")

    def require_human_constraint(self) -> HumanConstraintPin:
        raise RelationViolationError("Original wire message has no USER constraint pin")

    def require_annotation_grant(self):
        raise RelationViolationError("Original wire message has no human disclosure grant")

    def projections(self, message: messages.Message) -> Iterator[TypedTable]:
        """Search rows this attachment adds beside its original message: none."""
        return iter(())

    def team_update(self, recipient: ThreadLookup) -> tuple[TeamObservation, ...]:
        """The observations this attachment carries for that teammate birth: none."""
        return ()

    def category(self, spoken: type[MessageCategory]) -> type[MessageCategory]:
        """Which category the message carrying this attachment files under: its sender's."""
        return spoken

    async def delivered_by(self, bridge: DeliversInput[messages.Message], message: messages.Message) -> type[Outcome]:
        """How a harness bridge hands the message carrying this attachment to its session."""
        return await bridge.deliver(message)

    def message_segment(self, delivery: DeliveredContext) -> ContextSegment:
        """The same message contract for selected and following input."""
        return delivery.delivered()

    def annotation_rules(self, message: messages.Message, facts: retained_task_facts.RetainedTaskFacts) -> tuple[str, ...]:
        return ()

    def direct(self, goals: MessageDirections[messages.Message], message: messages.Message) -> None:
        """An owner message carrying a task is acted on by that task's declaration, not
        recorded as a request."""

    def original_text_source(self, message: messages.Message, originals: dict[MessageReference, messages.Message]) -> messages.Message:
        return message

    def original_wording(self, original: messages.Message) -> str:
        return original.body

    def original_wording_context_source(self, original: messages.Message) -> Provenance:
        return WireProvenance(original.reference)

    def original_input_sources(self, inputs: InputLookup[input_attempt.InputAttempt]) -> tuple[input_attempt.StoredInput, ...]:
        return ()

    def original_wording_provenance(self, original: messages.Message) -> dict[str, object]:
        return dict(wording=FieldCodec.encode(original.reference),
                    author=original.sender, author_role=FieldCodec.encode(original.sender_role))

    def selected_sources(self, message: messages.Message) -> tuple[messages.Message, ...]:
        return (message,)

    def lineage_reference(self, root: messages.Message) -> MessageReference:
        return root.reference

    def root_source(self, message: messages.Message, originals: dict[MessageReference, messages.Message]) -> messages.Message:
        raise RelationViolationError("Ordinary message is not a decision lineage event")

    def current_roots(self, message: messages.Message, originals: dict[MessageReference, messages.Message],
                      owner: TaskOwner, registry: TaskRegistry[TaskOwner]) -> tuple[messages.Message, ...]:
        return ()

    def revises_after(self, previous: messages.Message) -> bool:
        raise RelationViolationError("Ordinary message cannot revise a declared choice")

    def require_sender(self, sender: str) -> None:
        pass

    def require_target(self, target: str, registry: TaskRegistry[TaskOwner]) -> None:
        pass

    def require_publication(self, sender: str, registry: TaskRegistry[TaskOwner], original_source: MessageReferencesRead[messages.Message]) -> None:
        pass

    def retained_task_facts(self, message: messages.Message):
        return ()

    def user_task_facts(self, message: messages.Message):
        return ()


@dataclass(frozen=True)
class NoTaskAttachment(TaskAttachment, declared_name="absent"):
    def direct(self, goals: MessageDirections[messages.Message], message: messages.Message) -> None:
        """A plain owner message is a request wherever its destination holds them."""
        CommandTarget.named(message.target).direct(goals, message)
