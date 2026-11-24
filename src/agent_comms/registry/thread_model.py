from __future__ import annotations

from abc import (
    ABC,
    abstractmethod,
)
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Self

from pydantic import JsonValue

from agent_comms.execution.native_arguments import NativeArguments, ProviderChoice, SelectedModel
from agent_comms.execution.native_failures import NativePiUnavailable
from agent_comms.execution.pi_vocabulary import ServiceTier, ThinkingChoice
from agent_comms.nominal.field_codec import (
    DecoderPlan,
    ExternalField,
    SubclassUnion,
    WireValue,
    boundary_decoder,
    compile_decoder,
)
from agent_comms.registry.thread_text import AbsentText, GivenText, TextChoice


class ModelSelection(TextChoice):
    """A model's spelling and native arguments, shared by saved values and requests."""

    discovers_thinking_levels: ClassVar[bool] = True

    @abstractmethod
    def native(self, arguments: NativeArguments) -> NativeArguments: ...


class CliInboxSelection(ExternalField, WireValue, ModelSelection):
    """Selects external CLI-inbox execution instead of a native Pi provider/model."""

    __slots__ = ()
    wire_value: ClassVar[str] = "cli-inbox"
    choice_label: ClassVar[str] = "CLI inbox"
    choice_description: ClassVar[str] = "External CLI or MCP session on this thread's inbox"
    discovers_thinking_levels: ClassVar[bool] = False

    def otherwise[Default](self, default: Default) -> str:
        return self.wire_value

    def require(self, missing: str) -> str:
        return self.wire_value

    def texts(self) -> tuple[str, ...]:
        return (self.wire_value,)

    def native(self, arguments: NativeArguments) -> NativeArguments:
        return arguments

    @classmethod
    def wire_structure(cls, structural: DecoderPlan) -> DecoderPlan:
        return compile_decoder(str)

    @classmethod
    def decode_external_value(cls, value: object, record: Mapping[str, object]) -> object:
        return cls.from_wire(value)

    @classmethod
    def schema(cls) -> dict[str, JsonValue]:
        return {"const": cls.wire_value}

    def to_wire(self) -> str:
        return self.wire_value

    @classmethod
    def from_wire(cls, data: object) -> Self:
        if data == cls.wire_value:
            return cls()
        raise ValueError(f"Expected {cls.wire_value!r}")


@dataclass(frozen=True, slots=True)
class NamedModel(GivenText, ModelSelection):
    text: str

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("Thread model cannot be empty.")

    def native(self, arguments: NativeArguments) -> NativeArguments:
        return arguments.with_model(self.text)


class UnselectedModel(AbsentText, ModelSelection):
    def native(self, arguments: NativeArguments) -> NativeArguments:
        return arguments


class ThreadModel(SubclassUnion, ModelSelection, affix="Model"):
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

    def declared(self, text: str) -> DeclaredModel:
        """The model a launch argument names, replacing this one."""
        return DeclaredModel(text)

    @abstractmethod
    def applied(self, previous: ThreadModel) -> ThreadModel:
        """An undeclared model takes the previous or observed one; a declared one keeps itself."""

    @abstractmethod
    def set_model[Command](self, command: Callable[..., Command]) -> Command: ...

    @abstractmethod
    def require_reported(self) -> DeclaredModel:
        """A native runtime's configuration report must name its model."""

    @abstractmethod
    def qualified_by(self, provider: ProviderChoice) -> ThreadModel: ...

    def create_thread[Spec, Management, Created](
        self, spec: Spec, management: Management,
        native_create: Callable[[Spec], Created],
        cli_inbox_create: Callable[[Spec], Created],
    ) -> Created:
        return native_create(spec)


@dataclass(frozen=True, slots=True)
class DeclaredModel(NamedModel, ThreadModel):
    def __post_init__(self) -> None:
        super().__post_init__()
        if self.text == CliInboxSelection.wire_value:
            raise ValueError(f"{CliInboxSelection.wire_value!r} selects CLI inbox, not a native model.")

    def applied(self, previous: ThreadModel) -> DeclaredModel:
        return self

    def set_model[Command](self, command: Callable[..., Command]) -> Command:
        selected = SelectedModel.decode(self.text)
        return command(provider=selected.provider, model_id=selected.model)

    def require_reported(self) -> DeclaredModel:
        return self

    def qualified_by(self, provider: ProviderChoice) -> DeclaredModel:
        return DeclaredModel(provider.qualified(self.text))


@dataclass(frozen=True, slots=True)
class CliInboxThreadModel(CliInboxSelection, ThreadModel):
    """Explicit CLI-inbox selection in a thread model slot (e.g. + New Session or Configure)."""

    def applied(self, previous: ThreadModel) -> CliInboxThreadModel:
        return self

    def set_model[Command](self, command: Callable[..., Command]) -> Command:
        return command()

    def require_reported(self) -> DeclaredModel:
        raise NativePiUnavailable("CLI inbox thread has no native Pi model")

    def qualified_by(self, provider: ProviderChoice) -> CliInboxThreadModel:
        return self

    def create_thread[Spec, Management, Created](
        self, spec: Spec, management: Management,
        native_create: Callable[[Spec], Created],
        cli_inbox_create: Callable[[Spec], Created],
    ) -> Created:
        return cli_inbox_create(spec)


@dataclass(frozen=True, slots=True)
class UndeclaredModel(UnselectedModel, ThreadModel):
    def applied(self, previous: ThreadModel) -> ThreadModel:
        return previous

    def set_model[Command](self, command: Callable[..., Command]) -> Command:
        return command()

    def require_reported(self) -> DeclaredModel:
        raise NativePiUnavailable("Native configuration did not report a model")

    def qualified_by(self, provider: ProviderChoice) -> UndeclaredModel:
        return self


class CatalogSelection(ABC):
    """The settings and project supplied by the selection's actual owner."""

    __slots__ = ()

    @property
    @abstractmethod
    def project_path(self) -> Path: ...

    model: ThreadModel
    thinking_level: type[ThinkingChoice]
    service_tier: ServiceTier
