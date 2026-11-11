from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from agent_comms.errors import RejectedEnforcementError, TodoError
from agent_comms.registry.thread_identity import ThreadIncarnation


class TodoRefusal(TodoError, RejectedEnforcementError):
    """A todo refusal a standing rule reports as not applied, in the refusal's own words."""

    def explanation(self) -> str:
        return str(self)


class SharedWorktreeRefusal(TodoRefusal):
    """Two different order holders cannot write into the same physical worktree."""

    reason: ClassVar[str] = 'the proposed holder shares an occupied edit boundary'

    def __init__(self, problem: str, holding_problem: str, holder: ThreadIncarnation,
                 holding_holder: ThreadIncarnation, worktree: Path) -> None:
        self.problem = problem
        self.holding_problem = holding_problem
        self.holder = holder
        self.holding_holder = holding_holder
        self.worktree = worktree
        super().__init__(
            f"{problem} for {holder.name} shares worktree {worktree} with "
            f"{holding_problem} held by {holding_holder.name}."
        )


class TeammateOccupied(TodoRefusal):  # noqa: N818 - domain refusal
    """The unit holds an open order the incoming work does not own; a standing rule that
    restores work to it reports this refusal instead of applying."""

    reason: ClassVar[str] = "the teammate already holds other work"


class ForeignRequest(TodoRefusal):  # noqa: N818 - domain refusal
    """An owner request answers only its own teammates; a standing rule that restores work
    serving another teammate's request reports this refusal instead of ending the pass."""

    reason: ClassVar[str] = "the order serves another teammate's request"


class HolderConsentRequired(TodoRefusal):
    """Maintenance authority cannot replace an exact holder's consent."""

    reason: ClassVar[str] = "the current holder has not approved this work change"


class TodoStoreUnwritable(TodoError):  # noqa: N818 - domain-specific storage failure
    """The todo store could not complete its write; its original cause is preserved."""
