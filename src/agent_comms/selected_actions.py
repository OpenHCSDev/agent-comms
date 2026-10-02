"""Selected effects own instructions, native tool binding and post-model writes."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .coordination_errors import IdentityConflict
from .envelope_claim_transitions import ExistingFileClaim
from .turn_context import InstructionFile

if TYPE_CHECKING:
    from .channel_coding_tools import CodingToolOwner
    from .coordination_tables.assignments import WakeAssignment
    from .selected_tool_broker import NativeToolMode
    from .selected_write_authority import BoundSelectedWriteAuthority
    from .selected_write_plan import PlannedWrite
    from .threads import Thread


class SelectedAction(ABC):
    @property
    @abstractmethod
    def instruction_files(self) -> tuple[InstructionFile, ...]: ...

    def mode(self, owner: CodingToolOwner) -> NativeToolMode | None:
        return None

    def apply(self, owner: CodingToolOwner) -> None:
        return None

    def operation_id(self) -> str:
        import secrets

        return secrets.token_hex(16)

    def with_plan(
        self,
        authority: BoundSelectedWriteAuthority,
        plan: PlannedWrite,
        assignment: WakeAssignment,
        thread: Thread,
    ) -> SelectedAction:
        return PlannedSelectedWrite(authority, plan, assignment, thread)


class NoSelectedTools(SelectedAction):
    @property
    def instruction_files(self):
        return (InstructionFile.read("selected-no-tools.md"),)


@dataclass(frozen=True)
class BatchSelectedAction(SelectedAction):
    """Original operator plans stay bound to their own source in one native work turn."""

    originals: tuple[tuple[WakeAssignment, SelectedAction], ...]

    @property
    def instruction_files(self):
        return tuple(
            dict.fromkeys(
                instruction
                for _, action in self.originals
                for instruction in action.instruction_files
            )
        )

    def mode(self, owner):
        return next((mode for assignment, action in self.originals
                     if (mode := action.mode(owner.for_original(assignment, action.operation_id()))) is not None), None)

    def apply(self, owner):
        for assignment, action in self.originals:
            action.apply(owner.for_original(assignment, action.operation_id()))


class CodingSelectedAction(SelectedAction):
    @property
    def instruction_files(self):
        return (InstructionFile.read("selected-coding.md"),)

    def mode(self, owner: CodingToolOwner) -> NativeToolMode:
        from .channel_coding_tools import CodingToolMode

        return CodingToolMode(owner)


@dataclass(frozen=True, slots=True)
class SelectedExistingFileWrite(NoSelectedTools):
    """Trusted foreground replacement; no model tool or input authority."""

    resource: ExistingFileClaim
    contents: bytes

    def __post_init__(self) -> None:
        if type(self.resource) is not ExistingFileClaim or type(self.contents) is not bytes:
            raise TypeError("selected write needs an existing-file claim and bytes")
        if len(self.contents) > 1024 * 1024:
            raise ValueError("selected write exceeds 1 MiB")

    def with_plan(self, authority, plan, assignment, thread):
        raise IdentityConflict("Selected write has conflicting authority")

    def apply(self, owner: CodingToolOwner) -> None:
        from .claim_admission import publish_selected_resource_claim, write_selected_claimed_file

        claimed = publish_selected_resource_claim(
            owner.comms,
            owner.store,
            owner.admission,
            owner.owner_name,
            self.resource,
        )
        write_selected_claimed_file(
            owner.comms,
            owner.store,
            owner.admission,
            owner.owner_name,
            claimed,
            self.contents,
        )


@dataclass(frozen=True)
class PlannedSelectedWrite(NoSelectedTools):
    authority: BoundSelectedWriteAuthority
    plan: PlannedWrite
    assignment: WakeAssignment
    thread: Thread

    def operation_id(self) -> str:
        return self.plan.operation_id

    def apply(self, owner: CodingToolOwner) -> None:
        self.authority.require_current(self.assignment, self.thread, self.plan.operation_id)
        SelectedExistingFileWrite(self.plan.resource, self.plan.contents).apply(owner)
        self.authority.applied(self.assignment, self.thread, self.plan.operation_id)
