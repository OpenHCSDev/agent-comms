"""Native execution state shared by launch owners and selected input claims."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from .cursor_publication import CursorDelivery
from .native_package import VerifiedNativePackage
from .native_pi import NativePiRpcLaunch
from .pi_native_backend import PersistentPiSession


@dataclass(kw_only=True)
class NativeExecution(ABC):
    cursor_delivery: CursorDelivery
    pi_session: PersistentPiSession = field(default_factory=PersistentPiSession, compare=False)

    @abstractmethod
    def tracked_launch(self, package: VerifiedNativePackage, **options: object) -> NativePiRpcLaunch: ...


@dataclass(kw_only=True)
class PackagedNativeExecution(NativeExecution):
    native_package: VerifiedNativePackage
