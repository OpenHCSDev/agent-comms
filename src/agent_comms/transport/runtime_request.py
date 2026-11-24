from __future__ import annotations

from dataclasses import dataclass
from typing import (
    TYPE_CHECKING,
    Annotated,
    ClassVar,
)

from agent_comms.errors import RelationViolationError
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import WireOmitDefault
from agent_comms.registry.thread_identity import ThreadIncarnation
from agent_comms.registry.threads import Thread
from agent_comms.storage.maintenance_barrier import MaintenanceHolder, NoMaintenanceHolder
from agent_comms.transport.owner_connection import (
    AnyOwner,
    NoControllerToken,
    OwnerConnection,
    RequestedOwner,
)

if TYPE_CHECKING:
    from agent_comms.comms import Comms


@dataclass(frozen=True, kw_only=True)
class RuntimeRequest(
    DeclaredFamily,
    affix='RuntimeRequest',
):
    family_discriminator: ClassVar[str] = "action"
    thread: str
    holder: Annotated[MaintenanceHolder, WireOmitDefault()] = NoMaintenanceHolder()
    captured: Annotated[RequestedOwner, WireOmitDefault()] = AnyOwner()

    @classmethod
    def proxy_payload(
        cls, owner: ThreadIncarnation, controller_token: str | NoControllerToken,
        parameters: dict[str, object],
    ) -> dict[str, object]:
        return OwnerConnection.wire_request(cls.declared_name, owner, parameters)

    def held_by_bus(self, comms: Comms, thread: Thread) -> object:
        """The bus's own answer, for an owner whose state the bus holds (an external CLI; see
        `BusHeldThreadOwnerBinding`). Only a request about that state has one."""
        raise RelationViolationError(
            "This request needs a native owner runtime; the thread's CLI has none.")


class AttachedRuntimeRequest(
    RuntimeRequest,
    declared_name=None,
):
    pass


class ResultRuntimeRequest(
    RuntimeRequest,
    declared_name=None,
):
    pass


class AttachedResultRuntimeRequest(
    ResultRuntimeRequest,
    AttachedRuntimeRequest,
    declared_name=None,
):
    pass
