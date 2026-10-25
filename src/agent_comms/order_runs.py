"""The user-visible run that closes an order, and the evidence each kind of run accepts."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from agent_comms.declared_family import DeclaredFamily
from agent_comms.errors import ToolRefusedError

if TYPE_CHECKING:
    from agent_comms.build_identity import CommitInstallation
    from agent_comms.thread_identity import ThreadIncarnation
    from agent_comms.todos import MergedProblemOrder, Todo


class OrderRun(DeclaredFamily, affix="OrderRun"):
    label: ClassVar[str] = "Works when:"

    def require_assignment(
        self, unit: ThreadIncarnation, fired: tuple[Todo, ...], strike_limit: int
    ) -> None:
        """Mechanical work remains assignable after application work was taken away."""

    def require_artifact(self, path: str) -> None:
        pass

    def retexted(self, text: str) -> OrderRun:
        raise ToolRefusedError("A mechanical run has no text; its check alone closes the order.")

    def merged_line(self, installation: CommitInstallation) -> str:
        return f"Merged, {installation.merged_state}"

    @abstractmethod
    def after_merge(self, todo: Todo, merged: MergedProblemOrder) -> Todo: ...

    @abstractmethod
    def require_merge_close(self) -> None: ...

    @abstractmethod
    def instructions(self) -> tuple[str, ...]: ...

    @abstractmethod
    def require_closing(self, works: str) -> None:
        """Refuse closing evidence that does not show this run happened."""


@dataclass(frozen=True)
class MechanicalOrderRun(OrderRun):
    def after_merge(self, todo: Todo, merged: MergedProblemOrder) -> Todo:
        return merged.installation().after_merge(todo, merged)

    def require_merge_close(self) -> None:
        pass

    def instructions(self) -> tuple[str, ...]:
        return ()

    def require_closing(self, works: str) -> None:
        """The order's check alone closes it."""


@dataclass(frozen=True)
class ApplicationOrderRun(OrderRun):
    def require_assignment(
        self, unit: ThreadIncarnation, fired: tuple[Todo, ...], strike_limit: int
    ) -> None:
        if fired:
            raise ToolRefusedError(
                f"{unit.name} has {strike_limit} strikes on "
                f"{[todo.id for todo in fired]}. "
                f"Reassign them; only a mechanical run reaches {unit.name}."
            )

    def merged_line(self, installation: CommitInstallation) -> str:
        return f"{super().merged_line(installation)}, waiting on the run"

    def after_merge(self, todo: Todo, merged: MergedProblemOrder) -> Todo:
        return merged.waiting_in(todo)

    def require_merge_close(self) -> None:
        raise ToolRefusedError(
            "A merge never closes an application order; order close --works records the run."
        )

    def require_artifact(self, path: str) -> None:
        if not Path(path).is_file():
            raise ToolRefusedError("This order needs --seen FILE showing its requested run.")

    def retexted(self, text: str) -> OrderRun:
        return replace(self, text=text)

    text: str
    least_words: ClassVar[int] = 8  # the run, and the evidence that it happened
    quoted_words: ClassVar[int] = 4

    def __post_init__(self) -> None:
        if len(self.text.split()) < self.least_words:
            raise ToolRefusedError(
                f'Add "{self.label} <the user-visible closing run, '
                f'{self.least_words}+ words>"; a grep never closes a problem.'
            )

    def instructions(self) -> tuple[str, ...]:
        return (f"{self.label} {self.text}",)

    def require_closing(self, works: str) -> None:
        quoted = self.phrases(self.text) & self.phrases(works)
        if len(works.split()) < self.least_words or not quoted:
            raise ToolRefusedError(
                f"Describe what worked in {self.least_words}+ words, quoting "
                f"{self.quoted_words}+ consecutive words of: {self.label} {self.text}"
            )

    @classmethod
    def phrases(cls, text: str) -> set[tuple[str, ...]]:
        words = [word.strip(".,;:!?\"'`()").lower() for word in text.split()]
        return {
            tuple(words[at : at + cls.quoted_words])
            for at in range(len(words) - cls.quoted_words + 1)
        }
