"""Explicit producer declaration for the existing acquired owner read.

The release owner selects the authentic producer runtime and its format. Neither
member discovers formats from missing fields or offers a fallback reader.
"""
from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import JsonValue

from agent_comms.declared_family import DeclaredFamily
from agent_comms.field_codec import FieldCodec
from agent_comms.registry_document import RegistryDocument
from agent_comms.goal_history import GoalHistoryEntry
from agent_comms.owner_lifecycle import OwnerReleaseReceipt, OwnerReleaseStore, OwnerRestartSelection
if TYPE_CHECKING:
    from recorded_owner_read import RecordedOwnerRead


@dataclass(frozen=True)
class OriginalOwnerRead:
    """Current source values retain their declarations through stdio and decoding."""

    document: RegistryDocument
    selection: OwnerRestartSelection
    releases: dict[str, OwnerReleaseReceipt] = field(default_factory=dict)
    goal_history: tuple[GoalHistoryEntry, ...] = ()


@dataclass(frozen=True)
class RetiredOwnerRead:
    """Only codec-produced target postimages are opaque JSON, never live values."""

    registry: dict[str, JsonValue] = field(metadata={'wire_name': 'document'})
    selection: OwnerRestartSelection
    releases: dict[str, dict[str, JsonValue]]
    goal_history: tuple[GoalHistoryEntry, ...]


class OwnerReadProjection(DeclaredFamily, affix='Projection'):
    @classmethod
    def require_source_runtime(cls) -> None:
        """Current/recorded reads use their runtime's normal strict declaration."""

    @classmethod
    @abstractmethod
    def read(cls, registry, root: Path, name: str, request) -> OriginalOwnerRead | RetiredOwnerRead | RecordedOwnerRead:
        """Acquire evidence through its original declaration owner."""


class LiveOwnerReadProjection(OwnerReadProjection):
    @classmethod
    def read(cls, registry, root: Path, name: str, request) -> OriginalOwnerRead | RetiredOwnerRead:
        """Acquire the original live selection before projecting its document."""
        with registry.store.reading() as document:
            snapshot = document.snapshot()
            selection = (
                FieldCodec.decode(OwnerRestartSelection, request)
                if request is not None else OwnerRestartSelection.capture(snapshot, name)
            )
            selection.require_current(snapshot)
            return cls.project(root, registry.store.path, document, selection)

    @classmethod
    @abstractmethod
    def project(cls, root: Path, registry_path: Path, document: RegistryDocument,
                selection: OwnerRestartSelection) -> OriginalOwnerRead | RetiredOwnerRead:
        """Project an acquired selection without discarding its declarations."""


class RetiredGoalReportProjection(LiveOwnerReadProjection):
    @classmethod
    def require_source_runtime(cls) -> None:
        from thread_format_retirement import GoalReportMemberRetirement

        GoalReportMemberRetirement.require_original_declaration()

    @classmethod
    def project(cls, root: Path, registry_path: Path, document: RegistryDocument,
                selection: OwnerRestartSelection) -> RetiredOwnerRead:
        from thread_format_retirement import GoalReportMemberRetirement

        releases = OwnerReleaseStore(root / 'owner_release_receipts.json')
        with releases.locked():
            acquired = GoalReportMemberRetirement.acquire(
                registry_path, document, releases._read_unlocked())
            projected = acquired.project()
        projected.update(goal_history=acquired.history)
        return RetiredOwnerRead(selection=selection, **projected)


class CurrentThreadProjection(LiveOwnerReadProjection):
    @classmethod
    def project(cls, root: Path, registry_path: Path, document: RegistryDocument,
                selection: OwnerRestartSelection) -> OriginalOwnerRead:
        return OriginalOwnerRead(document, selection)


class RecordedRegistryProjection(OwnerReadProjection):
    """Original typed values projected by the target's recorded declaration."""

    @classmethod
    def read(cls, registry, root: Path, name: str, request) -> RecordedOwnerRead:
        """Only this selected capability imports the recorded-source SDK."""
        from recorded_owner_read import RecordedOwnerRead

        with registry.store.reading() as document:
            return RecordedOwnerRead.capture(root, registry.store.path, document, request)
