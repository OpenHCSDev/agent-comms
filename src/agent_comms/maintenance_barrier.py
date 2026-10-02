"""Default-off, wire-root maintenance admission for *new-code* callers only.

An enabled record does not constrain a process that imported older code. External
old-client exclusion and a Toad ingress pause are required before live use.
The enabled witness is written before the state and never removed; a lost or
corrupt state therefore denies admission instead of silently returning to OFF.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .declared_family import DeclaredFamily
from .field_codec import FieldCodec
from .lifecycle import LifecycleState

from .bus_publication import unique_wire_object
from .errors import RelationViolationError
from .store_files import _async_store_lock, _store_lock


class MaintenancePhase(DeclaredFamily, LifecycleState, affix="Phase"):
    @classmethod
    def require_open(cls) -> None:
        raise RelationViolationError("Maintenance admission closed")


class DrainingPhase(MaintenancePhase):
    @classmethod
    def successors(cls):
        return (PausedPhase,)


class PausedPhase(MaintenancePhase):
    @classmethod
    def successors(cls):
        return (InstallingPhase,)


class InstallingPhase(MaintenancePhase):
    @classmethod
    def successors(cls):
        return (ReadyPhase,)


class ReadyPhase(MaintenancePhase):
    @classmethod
    def successors(cls):
        return (DrainingPhase,)

    @classmethod
    def require_open(cls) -> None:
        pass


@dataclass(frozen=True)
class MaintenanceReceipt:
    generation: int
    operator: str
    nonce: str
    phase: type[MaintenancePhase]


@dataclass(frozen=True, kw_only=True)
class MaintenanceMarker:
    version: Literal[1]
    root: str
    generation: int

    def __post_init__(self):
        if not 0 < self.generation < 1 << 63:
            raise ValueError("Maintenance generation is outside its allocation domain")


@dataclass(frozen=True, kw_only=True)
class MaintenanceState(MaintenanceMarker):
    operator: str
    nonce: str
    phase: type[MaintenancePhase]

    def __post_init__(self):
        super().__post_init__()
        if not self.operator or len(self.nonce) != 32:
            raise ValueError("Maintenance requires operator and nonce")

    def require_marker(self, marker: MaintenanceMarker, root: Path):
        if (
            marker
            != MaintenanceMarker(
                version=self.version, root=str(root.resolve()), generation=self.generation
            )
            or self.root != marker.root
        ):
            raise RelationViolationError("Maintenance witness inconsistent; admission closed")
        return MaintenanceReceipt(self.generation, self.operator, self.nonce, self.phase)


class MaintenanceBarrier:
    """One root's read-only admission gate; no in-process operator exists.

    Ordinary workers, ACP adapters and tools import this package under the
    same OS identity as the wire owner. A public in-package transition would
    therefore grant any caller permanent host-wide denial authority. Only a
    future separately protected control plane may write phase witnesses.
    """

    def __init__(self, registry_path: Path):
        self.registry_path = Path(registry_path)
        self.wire_path = self.registry_path.parent / "wire"
        self.marker_path = self.registry_path.parent / ".maintenance-enabled"
        self.state_path = self.registry_path.parent / ".maintenance-state"

    @staticmethod
    def _read(path: Path, declaration):
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            return None
        except OSError as error:
            raise RelationViolationError("Maintenance witness is inaccessible") from error
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or (os.name == "posix" and info.st_uid != os.geteuid())
                or (os.name == "posix" and stat.S_IMODE(info.st_mode) != 0o600)
                or info.st_nlink != 1
                or info.st_size > 4096
            ):
                raise RelationViolationError("Maintenance witness is not private and regular")
            with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
                descriptor = -1
                value = FieldCodec.decode(
                    declaration, json.load(stream, object_pairs_hook=unique_wire_object)
                )
        except (OSError, UnicodeError, ValueError) as error:
            raise RelationViolationError("Maintenance witness is invalid") from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        return value

    def current_unlocked(self) -> MaintenanceReceipt | None:
        """Call while holding the registry lock OR the wire lock.

        For a send, the wire lock stays held through stdin.write; for a claim,
        the registry lock spans the turn's durable transition. A phase change
        takes both locks in wire -> registry order.
        """
        marker = self._read(self.marker_path, MaintenanceMarker)
        state = self._read(self.state_path, MaintenanceState)
        if marker is None and state is None:
            return None  # Only fresh, never-enabled roots are default OFF.
        if marker is None or state is None:
            raise RelationViolationError("Maintenance witness incomplete; admission closed")
        return state.require_marker(marker, self.registry_path.parent)

    def assert_open_unlocked(self) -> MaintenanceReceipt | None:
        receipt = self.current_unlocked()
        if receipt is not None:
            receipt.phase.require_open()
        return receipt

    def read(self) -> MaintenanceReceipt | None:
        with _store_lock(self.wire_path, shared=True):
            return self.current_unlocked()

    @contextmanager
    def admit_ingress(self, *, blocking: bool = True) -> Iterator[MaintenanceReceipt | None]:
        """Share open-root custody through one synchronous ingress operation.

        Do not await while inside this context. A child still needs to perform
        its own claim/send checks, and old-import children are not protected.
        Maintenance, stop and rename take exclusive wire custody. Independent
        inputs share this gate; their registry, input and journal owners retain
        their own determining locks.
        """
        with _store_lock(self.wire_path, shared=True, blocking=blocking):
            yield self.assert_open_unlocked()

    @asynccontextmanager
    async def admit_ingress_async(self):
        """Acquire the same ingress gate without blocking a native event loop.

        The borrower writes synchronously, then releases before waiting for
        pipe drain or a native response.
        """
        async with _async_store_lock(self.wire_path, shared=True):
            yield self.assert_open_unlocked()
