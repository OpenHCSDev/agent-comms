"""The model a thread declares: a provider/model name, or none, left to its runtime."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agent_comms.field_codec import SubclassUnion, boundary_decoder
from agent_comms.native_arguments import NativeArguments, SelectedModel
from agent_comms.native_failures import NativePiUnavailable
from agent_comms.thread_text import AbsentText, GivenText, TextChoice

if TYPE_CHECKING:
    from agent_comms.native_arguments import ProviderChoice
    from agent_comms.pi_commands import SetModel


class ThreadModel(SubclassUnion, TextChoice, affix="Model"):
    """A thread's declared model; an undeclared one is observed from its runtime."""

    @staticmethod
    @boundary_decoder
    def reported(name: str | None) -> ThreadModel:
        """The model an external producer reports by name, or none."""
        return ThreadModel.from_wire(name)

    @staticmethod
    @boundary_decoder
    def selected_by(arguments: NativeArguments) -> ThreadModel:
        """The model a launch's native arguments select, or none."""
        selected = arguments.last(lambda entry: entry.apply_model)(UndeclaredModel())
        return selected.qualified_by(arguments.provider)

    @staticmethod
    @boundary_decoder
    def from_choice(choice: tuple[str, str] | None) -> ThreadModel:
        """A saved session's provider and model, or none."""
        return UndeclaredModel() if choice is None else DeclaredModel("/".join(choice))

    def declared(self, text: str) -> DeclaredModel:
        """The model a launch argument names, replacing this one."""
        return DeclaredModel(text)

    @abstractmethod
    def native(self, arguments: NativeArguments) -> NativeArguments:
        """The native arguments selecting this model."""

    @abstractmethod
    def applied(self, previous: ThreadModel) -> ThreadModel:
        """An undeclared model takes the previous or observed one; a declared one keeps itself."""

    @abstractmethod
    def set_model(self, command: type[SetModel]) -> SetModel: ...

    @abstractmethod
    def require_reported(self) -> DeclaredModel:
        """A native runtime's configuration report must name its model."""

    @abstractmethod
    def qualified_by(self, provider: ProviderChoice) -> ThreadModel: ...


@dataclass(frozen=True, slots=True)
class DeclaredModel(GivenText, ThreadModel):
    text: str

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("Thread model cannot be empty.")

    def native(self, arguments: NativeArguments) -> NativeArguments:
        return arguments.with_model(self.text)

    def applied(self, previous: ThreadModel) -> DeclaredModel:
        return self

    def set_model(self, command: type[SetModel]) -> SetModel:
        selected = SelectedModel.decode(self.text)
        return command(provider=selected.provider, model_id=selected.model)

    def require_reported(self) -> DeclaredModel:
        return self

    def qualified_by(self, provider: ProviderChoice) -> DeclaredModel:
        return DeclaredModel(provider.qualified(self.text))


@dataclass(frozen=True, slots=True)
class UndeclaredModel(AbsentText, ThreadModel):
    def native(self, arguments: NativeArguments) -> NativeArguments:
        return arguments

    def applied(self, previous: ThreadModel) -> ThreadModel:
        return previous

    def set_model(self, command: type[SetModel]) -> SetModel:
        return command()

    def require_reported(self) -> DeclaredModel:
        raise NativePiUnavailable("Native configuration did not report a model")

    def qualified_by(self, provider: ProviderChoice) -> UndeclaredModel:
        return self
