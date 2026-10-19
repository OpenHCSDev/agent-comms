"""Selected effects own instructions, native tool binding and post-model writes."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .coordination_errors import IdentityConflict
from .envelope_claim_transitions import ExistingFileClaim
from .field_codec import Id128Text
from .native_tool_call import NativeToolMode
from .native_tool_transport import ToolTransport
from .turn_context import InstructionFile

if TYPE_CHECKING:
    from .channel_coding_tools import CodingToolOwner
    from .coordination_tables.assignments import WakeAssignment
    from .selected_write_authority import BoundSelectedWriteAuthority
    from .selected_write_plan import PlannedWrite
    from .threads import Thread


class SelectedAction(ABC):
    @property
    @abstractmethod
    def instruction_files(self) -> tuple[InstructionFile, ...]: ...

    @abstractmethod
    def mode(self, owner: CodingToolOwner) -> NativeToolMode: ...

    def apply(self, owner: CodingToolOwner) -> None:
        return None

    def prepare(self, thread: Thread) -> SelectedAction:
        """Validate and bind action-owned resources before any native request."""
        return self

    def operation_id(self) -> str:
        return Id128Text.mint()

    def with_plan(
        self,
        authority: BoundSelectedWriteAuthority,
        plan: PlannedWrite,
        assignment: WakeAssignment,
        thread: Thread,
    ) -> SelectedAction:
        return PlannedSelectedWrite(authority, plan, assignment, thread)


class NoSelectedTools(SelectedAction, NativeToolMode):
    """This action explicitly declares no model tools; it still opens an endpoint."""

    def mode(self, owner):
        return self

    def launch_arguments(self, package):
        return ("--no-tools",)

    async def open_transport(self, turn, ownership):
        return await ToolTransport.open(turn, ownership)

    def or_else(self, factory):
        return factory()

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

    def __post_init__(self):
        if not self.originals:
            raise ValueError("A selected batch requires an original action")

    def mode(self, owner):
        assignment, action = self.originals[0]
        mode = action.mode(owner.for_original(assignment, action.operation_id()))
        for assignment, action in self.originals[1:]:
            mode = mode.or_else(
                lambda: action.mode(owner.for_original(assignment, action.operation_id()))
            )
        return mode

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

    def prepare(self, thread: Thread) -> SelectedAction:
        worktree = Path(thread.worktree).absolute()
        normalized = self.resource.normalized(worktree)
        try:
            Path(normalized).relative_to(worktree)
        except ValueError as error:
            raise ValueError(
                "Selected write is outside this thread's project directory"
            ) from error
        return SelectedExistingFileWrite(ExistingFileClaim(Path(normalized)), self.contents)

    def apply(self, owner: CodingToolOwner) -> None:
        from .selected_claim import publish_selected_resource_claim, write_selected_claimed_file

        claimed = publish_selected_resource_claim(
            owner.comms,
            owner.store,
            owner.claim_scope,
            owner.owner_name,
            self.resource,
        )
        write_selected_claimed_file(
            owner.comms,
            owner.store,
            owner.claim_scope,
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
