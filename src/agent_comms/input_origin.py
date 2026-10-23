"""Record where input came from without granting permission to run or replay it."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated


from .bus_publication import HumanOrigin
from .declared_family import DeclaredFamily
from .errors import RelationViolationError
from .field_codec import Id128Text
from .goals import GoalCheckpoint
from .message_reference import MessageReference
from .thread_identity import ClaimedIdentity
from .turn_context import Provenance

if TYPE_CHECKING:
    from .comms import Comms
    from .input_attempt import StoredInput
    from .registry_document import RegistrySnapshot
    from .threads import Thread


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

    def require_ingress(self, comms: Comms, snapshot: RegistrySnapshot,
                        claim_scope: ClaimedIdentity, controller) -> None:
        pass

    def retained_fact(self, source: StoredInput):
        from .retained_task_facts import InputTaskFact

        return InputTaskFact(source)

    def require_human(self) -> HumanInputOrigin:
        raise RelationViolationError("Input lacks an original human author witness")

    def matches_owner(self, recorded_name, incarnation):
        return recorded_name == incarnation.name

    def delivery_reference(self, root_id: str) -> InputDelivery:
        return UndeliveredInput()

    @property
    def reply_target(self) -> str | None:
        """Automatic native destination declared by this original ingress."""
        return None


@dataclass(frozen=True)
class InputProvenance(Provenance):
    """Original identity and author coordinate, independent of disposition."""
    key: str
    origin: InputOrigin

    def public_description(self) -> str:
        return f"Original input {self.key} · {self.origin.declared_name}"

    def public_text(self, comms) -> str:
        from .input_disposition import InputDispositions

        return self.require_original(InputDispositions(comms.root)).source_text

    def require_human_input(self):
        self.origin.require_human()
        return self

    def require_original(self, inputs):
        """Look up the input by key and check its recorded author."""
        return inputs.lookup(self.key).require_original_provenance(self)


@dataclass(frozen=True)
class UnattributedInputOrigin(InputOrigin):
    """No recorded author proof; exact text remains neutral evidence."""


@dataclass(frozen=True)
class WireInputOrigin(InputOrigin):
    root_id: Annotated[str, Id128Text]
    reference: MessageReference

    def delivery_reference(self, root_id: str) -> InputDelivery:
        return DeliveredInput(self.reference) if self.root_id == root_id else UndeliveredInput()

    def require_ingress(self, comms, snapshot, claim_scope, controller):
        raise RelationViolationError("Wire input provenance belongs to its original routed producer")


@dataclass(frozen=True)
class HumanInputOrigin(InputOrigin):
    """Same cooperative local USER contract as the original wire publisher.

    The attached controller proves only who holds the transport. This USER
    declaration supplies authorship, not executable or tool authority.
    """

    root_id: Annotated[str, Id128Text]
    author: HumanOrigin
    claimed_identity: ClaimedIdentity
    project: str
    goal: GoalCheckpoint

    @classmethod
    def capture(cls, comms: Comms, claim_scope: ClaimedIdentity) -> HumanInputOrigin:
        from .active_route import guard_original_root_write

        with guard_original_root_write(comms.root), comms.registry.store.locked():
            snapshot = comms.registry.store.read_snapshot_unlocked()
            owner = snapshot.require_active(claim_scope.incarnation.name)
            if snapshot.claimed_identity(owner.name) != claim_scope:
                raise RelationViolationError("User input attachment changed before capture")
            root_id = comms.registry.store.private_guard_unlocked().namespace
            root_id = Id128Text.decode(root_id)
            user = comms.messaging.user_identity(owner.worktree)
            return cls(root_id, HumanOrigin.of(user),
                       claim_scope, owner.worktree, owner.goal.captured)

    @property
    def reply_target(self) -> str:
        return self.author.sender

    def require_ingress(self, comms, snapshot, claim_scope, controller):
        from .runtime import UNBOUND_CONTROLLER

        if controller is None or controller is UNBOUND_CONTROLLER:
            raise RelationViolationError("User input origin requires its attached ACP controller")
        from .store_files import SharedLock

        with comms.registry.store.locked(mode=SharedLock()):
            comms.registry.store.private_guard_unlocked().require_root(
                self.root_id, RelationViolationError("User input origin belongs to another native namespace"),
            )
        if self.claimed_identity != claim_scope:
            raise RelationViolationError("User input origin attachment changed")
        self.author.require_registered(snapshot)
        owner = snapshot.require_active(claim_scope.incarnation.name)
        if (self.project, self.goal) != (owner.worktree, owner.goal.captured):
            raise RelationViolationError("User input scope changed before reservation")

    def retained_fact(self, source):
        from .retained_task_facts import HumanInputTaskFact

        return HumanInputTaskFact(source)

    def require_human(self) -> HumanInputOrigin:
        return self

    def applies(self, owner: Thread, snapshot: RegistrySnapshot) -> bool:
        return (self.claimed_identity.incarnation.resolved(snapshot) == owner.incarnation
                and (self.project, self.goal) == (owner.worktree, owner.goal.captured))

    def matches_owner(self, recorded_name, incarnation):
        """The captured original birth survives a rename, unlike a neutral name."""
        authorized = self.claimed_identity.incarnation.require_recorded()
        original = incarnation.require_recorded()
        return (recorded_name == authorized.name
                and authorized.created_at == original.created_at)
