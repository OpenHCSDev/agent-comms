from __future__ import annotations

from dataclasses import dataclass
from typing import (
    Annotated,
    ClassVar,
)

from ..nominal.declared_family import DeclaredFamily
from ..nominal.field_codec import WireOmitDefault
from ..owner_connection import (
    AnyOwner,
    NoControllerToken,
    OwnerConnection,
    RequestedOwner,
)
from ..storage.maintenance_barrier import (
    MaintenanceHolder,
    NoMaintenanceHolder,
)
from ..registry.thread_identity import ThreadIncarnation


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
