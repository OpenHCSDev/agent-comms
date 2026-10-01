"""Selected effects own instructions, native tool binding and post-model writes."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .coordination_errors import IdentityConflict
from .envelope_claim_transitions import ExistingFileClaim

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
    def instruction(self) -> str: ...

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
    instruction = "Answer the original message directly and concisely, using no tools. "


@dataclass(frozen=True)
class BatchSelectedAction(SelectedAction):
    """Original operator plans stay bound to their own source in one native work turn."""

    originals: tuple[tuple[WakeAssignment, SelectedAction], ...]

    @property
    def instruction(self) -> str:
        return " ".join(dict.fromkeys(action.instruction for _, action in self.originals))

    def mode(self, owner):
        return next((mode for _, action in self.originals
                     if (mode := action.mode(owner)) is not None), None)

    def apply(self, owner):
        for assignment, action in self.originals:
            action.apply(owner.for_original(assignment, action.operation_id()))


class CodingSelectedAction(SelectedAction):
    instruction = (
        "Answer the committed request and do the requested work using the normal "
        "read, bash, edit and write tools. "
        "Edit/write claims are checked by the owner before execution. "
        "Bash is cooperative: respect other agents' claims, stay in your worktree, "
        "and do not bypass a denied edit through shell. "
        "Never retry a tool or input with UNKNOWN outcome; report the concrete failure. "
        "Your final answer is published automatically to the original reply target. "
        "Return the answer directly; do not launch another agent or send a duplicate reply. "
        "Finish with the actual result and tests, not a promise of later work. "
    )

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
