"""Native execution state shared by launch owners and selected input claims."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from agent_comms.execution.cursor_publication import CursorDelivery
from agent_comms.execution.native_pi import NativePiRpcLaunch
from agent_comms.execution.persistent_pi_session import PersistentPiSession
from agent_comms.installer.native_package import VerifiedNativePackage


@dataclass(kw_only=True)
class NativeExecution(ABC):
    cursor_delivery: CursorDelivery
    pi_session: PersistentPiSession = field(default_factory=PersistentPiSession, compare=False)

    @abstractmethod
    def tracked_launch(self, package: VerifiedNativePackage, **options: object) -> NativePiRpcLaunch: ...


@dataclass(kw_only=True)
class RunsNativePackage:
    """The verified native package an execution runs."""

    __slots__ = ()

    native_package: VerifiedNativePackage


@dataclass(kw_only=True)
class PackagedNativeExecution(RunsNativePackage, NativeExecution):
    pass
