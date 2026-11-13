"""Record where input came from without granting permission to run or replay it."""

from abc import abstractmethod
from dataclasses import dataclass
from typing import Annotated

from agent_comms.errors import RelationViolationError
from agent_comms.messaging import initial_origin
from agent_comms.execution.controller import NO_CONTROLLER
from agent_comms.execution.input_ingress import InputIngressContext, InputRegistry
from agent_comms.execution.input_key import InputKey, InputKeyText
from agent_comms.execution.turn_context import Provenance
from agent_comms.installer.harness_install import HarnessSession
from agent_comms.messaging.message_reference import MessageReference
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import RootId
from agent_comms.planning.goals import GoalCheckpoint
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.task_owner import TaskOwner
from agent_comms.registry.task_registry import TaskRegistry
from agent_comms.registry.thread_identity import ClaimedIdentity, ClaimScoped
from agent_comms.transport.tool_result import ToolResult


class InputDelivery:
    """Whether an input delivered one bus message."""

    @abstractmethod
    def references(self) -> tuple[MessageReference, ...]: ...


@dataclass(frozen=True)
class DeliveredInput(InputDelivery):
    reference: MessageReference

    def references(self) -> tuple[MessageReference, ...]:
        return (self.reference,)


class UndeliveredInput(InputDelivery):
    def references(self) -> tuple[MessageReference, ...]:
        return ()


class InputOrigin(DeclaredFamily, affix="InputOrigin"):
    """The original reservation owns this source through every disposition."""

    def require_ingress[Owner: TaskOwner](
        self, comms: InputIngressContext[Owner], snapshot: InputRegistry[Owner],
        claim_scope: ClaimedIdentity, controller,
    ) -> None:
        pass

    def require_human(self) -> 'HumanInputOrigin':
        raise RelationViolationError("Input lacks an original human author witness")

    def harness_sources(self) -> tuple[HarnessAttemptOrigin, ...]:
        """An origin without a harness witness contributes no harness sources."""
        return ()

    def matches_owner(self, recorded_name, incarnation):
        return recorded_name == incarnation.name

    def delivery_reference(self, root_id: RootId) -> InputDelivery:
        return UndeliveredInput()

    @property
    def reply_target(self) -> str | None:
        """Automatic native destination declared by this original ingress."""
        return None


@dataclass(frozen=True)
class InputProvenance(Provenance):
    """Original identity and author coordinate, independent of disposition."""
    key: Annotated[InputKey, InputKeyText]
    origin: InputOrigin

    def public_description(self) -> str:
        return f"Original input {self.key} · {self.origin.declared_name}"

    def public_text(self, comms) -> str:
        inputs = comms.input_dispositions(comms.root)
        return inputs.lookup(self.key).require_original_provenance(self).source_text

    def require_human_input(self):
        self.origin.require_human()
        return self


@dataclass(frozen=True)
class UnattributedInputOrigin(InputOrigin):
    """No recorded author proof; exact text remains neutral evidence."""


@dataclass(frozen=True)
class WireInputOrigin(InputOrigin):
    root_id: RootId
    reference: MessageReference

    def delivery_reference(self, root_id: RootId) -> InputDelivery:
        return DeliveredInput(self.reference) if self.root_id == root_id else UndeliveredInput()

    def require_ingress(self, comms, snapshot, claim_scope, controller):
        raise RelationViolationError("Wire input provenance belongs to its original routed producer")


@dataclass(frozen=True)
class HarnessAttemptOrigin(InputOrigin):
    """A harness bridge attempted this message and never confirmed it entered context.

    It was never a native ingress, so it inherits UndeliveredInput: native recovery,
    which replays only unbound wire deliveries, can never take it.
    """

    bridge: type[HarnessSession]
    reference: MessageReference

    def harness_sources(self) -> tuple[HarnessAttemptOrigin, ...]:
        return (self,)


@dataclass(frozen=True)
class HumanInputOrigin(InputOrigin, ClaimScoped, ToolResult):
    """Same cooperative local USER contract as the original wire publisher.

    The attached controller proves only who holds the transport. This USER
    declaration supplies authorship, not executable or tool authority.
    """

    root_id: RootId
    author: initial_origin.HumanOrigin
    project: Checkout
    goal: GoalCheckpoint

    @classmethod
    def capture[Owner: TaskOwner](
        cls, comms: InputIngressContext[Owner], claim_scope: ClaimedIdentity,
    ) -> HumanInputOrigin:
        cut = comms.registry.store.snapshot_with_authority()
        owner = cls._require_owner(cut.snapshot, claim_scope)
        root_id = cut.privacy.namespace
        comms.messaging.user_identity(owner.worktree)
        cut = comms.registry.store.snapshot_with_authority()
        cut.privacy.require_root(root_id, RelationViolationError("User input namespace changed before capture"))
        owner = cls._require_owner(cut.snapshot, claim_scope)
        user = cut.snapshot.user_thread().require(RelationViolationError("User input author was removed"))
        return cls(
            root_id=root_id, author=initial_origin.HumanOrigin.of(user),
            claimed_identity=claim_scope, project=owner.worktree, goal=owner.goal.captured,
        )

    @staticmethod
    def _require_owner[Owner: TaskOwner](
        snapshot: InputRegistry[Owner], claim_scope: ClaimedIdentity,
    ) -> Owner:
        owner = snapshot.require_active(claim_scope.incarnation.name)
        if snapshot.claimed_identity(owner.name) != claim_scope:
            raise RelationViolationError("User input attachment changed before capture")
        return owner

    @property
    def reply_target(self) -> str:
        return self.author.sender

    def require_ingress(self, comms, snapshot, claim_scope, controller):
        controller.attached(NO_CONTROLLER).require_present(
            RelationViolationError("User input origin requires its attached ACP controller"))

        comms.registry.store.privacy().require_root(
            self.root_id, RelationViolationError("User input origin belongs to another native namespace"),
        )
        if self.claimed_identity != claim_scope:
            raise RelationViolationError("User input origin attachment changed")
        self.author.require_registered(snapshot)
        owner = self._require_owner(snapshot, claim_scope)
        if (self.project, self.goal) != (owner.worktree, owner.goal.captured):
            raise RelationViolationError("User input scope changed before reservation")

    def require_human(self) -> HumanInputOrigin:
        return self

    def applies(self, owner: TaskOwner, snapshot: TaskRegistry[TaskOwner]) -> bool:
        return (self.claimed_identity.incarnation.resolved(snapshot) == owner.incarnation
                and (self.project, self.goal) == (owner.worktree, owner.goal.captured))

    def matches_owner(self, recorded_name, incarnation):
        """The captured original birth survives a rename, unlike a neutral name."""
        authorized = self.claimed_identity.incarnation.require_recorded()
        original = incarnation.require_recorded()
        return (recorded_name == authorized.name
                and authorized.created_at == original.created_at)


