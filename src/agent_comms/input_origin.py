"""Original input provenance; never an execution, replay or permission capability."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated

from .bus_publication import HumanOrigin
from .declared_family import DeclaredFamily
from .errors import RelationViolationError
from .goals import GoalCheckpoint
from .message_reference import MessageReference
from .thread_identity import AdmissionIdentity
from .wire_metadata import WireRootIdText
from .turn_context import Provenance

if TYPE_CHECKING:
    from .comms import Comms
    from .input_attempt import StoredInput
    from .registry_document import RegistrySnapshot
    from .threads import Thread


class InputOrigin(DeclaredFamily, affix="InputOrigin"):
    """The original reservation owns this source through every disposition."""

    def require_ingress(self, comms: Comms, snapshot: RegistrySnapshot,
                        admission: AdmissionIdentity, controller) -> None:
        pass

    def retained_fact(self, source: StoredInput):
        from .retained_task_facts import InputTaskFact

        return InputTaskFact(source)

    def require_human(self) -> HumanInputOrigin:
        raise RelationViolationError("Input lacks an original human author witness")

    def matches_owner(self, recorded_name, incarnation):
        return recorded_name == incarnation.name


@dataclass(frozen=True)
class InputProvenance(Provenance):
    """Original identity and author coordinate, independent of disposition."""
    key: str
    origin: InputOrigin

    def require_human_input(self):
        self.origin.require_human()
        return self

    def require_original(self, inputs):
        """Resolve the recorded author and key in the original input document."""
        return inputs.lookup(self.key).require_original_provenance(self)


@dataclass(frozen=True)
class UnattributedInputOrigin(InputOrigin):
    """No recorded author proof; exact text remains neutral evidence."""


@dataclass(frozen=True)
class WireInputOrigin(InputOrigin):
    root_id: Annotated[str, WireRootIdText]
    reference: MessageReference

    def require_ingress(self, comms, snapshot, admission, controller):
        raise RelationViolationError("Wire input provenance belongs to its original routed producer")


@dataclass(frozen=True)
class HumanInputOrigin(InputOrigin):
    """Same cooperative local USER contract as the original wire publisher.

    The attached controller proves only transport custody. This original USER
    declaration supplies authorship, not executable or tool authority.
    """

    root_id: Annotated[str, WireRootIdText]
    author: HumanOrigin
    admission: AdmissionIdentity
    project: str
    goal: GoalCheckpoint

    @classmethod
    def capture(cls, comms: Comms, admission: AdmissionIdentity) -> HumanInputOrigin:
        from .active_route import guard_original_root_write
        from .store_files import _store_lock

        with guard_original_root_write(comms.root), _store_lock(comms._wire_lock_path):
            snapshot = comms.registry.snapshot()
            owner = snapshot.require_active(admission.incarnation.name)
            if snapshot.admission_identity(owner.name) != admission:
                raise RelationViolationError("User input attachment changed before capture")
            root_id = comms.bus.log.read_metadata_unlocked().wire_root_id
            root_id = WireRootIdText.decode(root_id)
            user = comms.messaging._user_identity_under_wire_lock(owner.worktree)
            return cls(root_id, HumanOrigin(user.name, user.created_at, user.worktree),
                       admission, owner.worktree, owner.goal_checkpoint)

    def require_ingress(self, comms, snapshot, admission, controller):
        from .runtime import UNBOUND_CONTROLLER

        if controller is None or controller is UNBOUND_CONTROLLER:
            raise RelationViolationError("User input origin requires its attached ACP controller")
        if self.root_id != comms.bus.log.read_metadata_unlocked().wire_root_id:
            raise RelationViolationError("User input origin belongs to another wire root")
        if self.admission != admission:
            raise RelationViolationError("User input origin attachment changed")
        self.author.require_registered(snapshot)
        owner = snapshot.require_active(admission.incarnation.name)
        if (self.project, self.goal) != (owner.worktree, owner.goal_checkpoint):
            raise RelationViolationError("User input scope changed before reservation")

    def retained_fact(self, source):
        from .retained_task_facts import HumanInputTaskFact

        return HumanInputTaskFact(source)

    def require_human(self) -> HumanInputOrigin:
        return self

    def applies(self, owner: Thread, snapshot: RegistrySnapshot) -> bool:
        return (self.admission.incarnation.resolved(snapshot) == owner.incarnation
                and (self.project, self.goal) == (owner.worktree, owner.goal_checkpoint))

    def matches_owner(self, recorded_name, incarnation):
        """The captured original birth survives a rename, unlike a neutral name."""
        self.admission.incarnation.require_recorded()
        incarnation.require_recorded()
        return (recorded_name == self.admission.incarnation.name
                and self.admission.incarnation.created_at == incarnation.created_at)
