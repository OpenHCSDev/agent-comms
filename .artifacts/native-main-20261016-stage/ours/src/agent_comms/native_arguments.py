"""Owned Pi launch options; unknown external arguments remain with Pi's parser."""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
import shlex
from typing import ClassVar

from .declared_family import DeclaredFamily
from .field_codec import FieldCodec, WireValue
from pathlib import Path

@dataclass(frozen=True)
class NativeArgument(DeclaredFamily, affix="Argument"):
    @property
    @abstractmethod
    def argv(self) -> tuple[str, ...]: ...

    def validate_rpc(self) -> None:
        pass

    def owned_turn(self) -> tuple[NativeArgument, ...]:
        return (self,)


@dataclass(frozen=True)
class ExternalArgument(NativeArgument):
    tokens: tuple[str, ...]

    @property
    def argv(self) -> tuple[str, ...]:
        return self.tokens


class OptionArgument(NativeArgument):
    short: ClassVar[str | None] = None

    @classmethod
    @abstractmethod
    def flag(cls) -> str: ...

    @classmethod
    @abstractmethod
    def consume(cls, inline: str | None, arguments: Iterator[str]) -> OptionArgument: ...


class NamedOption:
    @classmethod
    def flag(cls) -> str:
        return "--" + cls.declared_name.replace("_", "-")


@dataclass(frozen=True)
class ValueArgument(OptionArgument):
    value: str

    @classmethod
    def consume(cls, inline: str | None, arguments: Iterator[str]) -> ValueArgument:
        value = inline if inline is not None else next(arguments, None)
        if value is None:
            raise ValueError(f"{cls.flag()} requires a value")
        return cls(value)

    @property
    def argv(self) -> tuple[str, ...]:
        return self.flag(), self.value


class ProviderArgument(NamedOption, ValueArgument):
    pass


class ModelArgument(NamedOption, ValueArgument):
    def qualified(self, provider: str | None) -> str:
        if provider is None or self.value.startswith(provider + "/"):
            return self.value
        return provider + "/" + self.value


class ThinkingArgument(NamedOption, ValueArgument):
    pass


class ExtensionArgument(NamedOption, ValueArgument):
    short = "-e"


class SessionSelectionArgument:
    """Managed RPC source selection belongs to the captured SelectedSession.

    The native CLI still accepts these external options. A configured argument
    cannot independently replace the original owner selection in managed RPC.
    """

    def validate_rpc(self) -> None:
        raise ValueError("Managed Pi session selection belongs to SelectedSession")


class SessionArgument(SessionSelectionArgument, NamedOption, ValueArgument):
    pass


class SessionDirArgument(SessionSelectionArgument, NamedOption, ValueArgument):
    pass


class SessionIdArgument(SessionSelectionArgument, NamedOption, ValueArgument):
    pass


class ForkArgument(SessionSelectionArgument, NamedOption, ValueArgument):
    pass


class ModeArgument(NamedOption, ValueArgument):
    def validate_rpc(self) -> None:
        if self.value != "rpc":
            raise ValueError("Managed Pi requires RPC mode")


class FlagArgument(OptionArgument):
    @classmethod
    def consume(cls, inline: str | None, arguments: Iterator[str]) -> FlagArgument:
        if inline is not None:
            raise ValueError(f"{cls.flag()} does not accept a value")
        return cls()

    @property
    def argv(self) -> tuple[str, ...]:
        return (self.flag(),)


class OneShotArgument(FlagArgument):
    def validate_rpc(self) -> None:
        raise ValueError("Managed Pi cannot run a one-shot CLI command")


class TurnToolArgument:
    """The bound turn mode replaces startup tool choices, not discovery settings."""

    def owned_turn(self) -> tuple[NativeArgument, ...]:
        return ()


class ToolsArgument(TurnToolArgument, NamedOption, ValueArgument):
    short = "-t"


class ExcludeToolsArgument(TurnToolArgument, NamedOption, ValueArgument):
    short = "-xt"


class NoToolsArgument(TurnToolArgument, NamedOption, FlagArgument):
    short = "-nt"


class NoBuiltinToolsArgument(TurnToolArgument, NamedOption, FlagArgument):
    short = "-nbt"


class ContinueArgument(SessionSelectionArgument, NamedOption, OneShotArgument):
    short = "-c"


class ResumeArgument(SessionSelectionArgument, NamedOption, OneShotArgument):
    short = "-r"


class NoSessionArgument(SessionSelectionArgument, NamedOption, OneShotArgument):
    pass


class PrintArgument(NamedOption, OneShotArgument):
    short = "-p"


class HelpArgument(NamedOption, OneShotArgument):
    short = "-h"


class VersionArgument(NamedOption, OneShotArgument):
    short = "-v"


@dataclass(frozen=True)
class NativeArguments(WireValue):
    entries: tuple[NativeArgument, ...] = ()

    @classmethod
    def from_wire(cls, data: object) -> NativeArguments:
        return cls.parse(FieldCodec.decode(tuple[str, ...], data))

    def to_wire(self) -> tuple[str, ...]:
        return self.argv

    @classmethod
    def schema(cls) -> dict[str, object]:
        return FieldCodec.value_schema(tuple[str, ...])

    @classmethod
    def parse(cls, arguments: Sequence[str] | str) -> NativeArguments:
        if isinstance(arguments, str):
            arguments = shlex.split(arguments)
        options = {
            flag: member
            for member in OptionArgument.members_with(OptionArgument)
            for flag in (member.flag(), member.short)
            if flag is not None
        }
        result = []
        source = iter(arguments)
        for token in source:
            name, separator, value = token.partition("=")
            member = options.get(name)
            result.append(
                member.consume(value if separator else None, source)
                if member is not None
                else ExternalArgument((token,))
            )
        return cls(tuple(result))

    @property
    def argv(self) -> tuple[str, ...]:
        return tuple(token for entry in self.entries for token in entry.argv)

    def value(self, member: type[ValueArgument]) -> str | None:
        return next(
            (entry.value for entry in reversed(self.entries) if isinstance(entry, member)), None
        )

    @property
    def model(self) -> str | None:
        selected = next(
            (entry for entry in reversed(self.entries) if isinstance(entry, ModelArgument)), None
        )
        return selected.qualified(self.value(ProviderArgument)) if selected is not None else None

    @property
    def thinking(self) -> str | None:
        return self.value(ThinkingArgument)

    def replace(self, member: type[NativeArgument], *entries: NativeArgument) -> NativeArguments:
        return NativeArguments(
            tuple(entry for entry in self.entries if not isinstance(entry, member)) + entries
        )

    def with_model(self, selected: str | None) -> NativeArguments:
        if selected is None or "/" not in selected:
            return self
        provider, model = selected.split("/", 1)
        return self.replace(ProviderArgument).replace(
            ModelArgument, ProviderArgument(provider), ModelArgument(model)
        )

    def with_thinking(self, selected: str | None) -> NativeArguments:
        return self.replace(ThinkingArgument, *((ThinkingArgument(selected),) if selected else ()))

    def for_owned_turn(self) -> NativeArguments:
        return NativeArguments(tuple(selected for entry in self.entries for selected in entry.owned_turn()))

    def with_extensions(self, *required: Path) -> NativeArguments:
        """Required integration loads first; each explicit entry is supplied once."""
        selected = dict.fromkeys((*map(str, required), *(entry.value for entry in self.entries
                                                       if isinstance(entry, ExtensionArgument))))
        return self.replace(ExtensionArgument, *(ExtensionArgument(value) for value in selected))

    def extended(self, *entries: NativeArgument) -> NativeArguments:
        return NativeArguments((*self.entries, *entries))

    def rpc(self) -> NativeArguments:
        for entry in self.entries:
            entry.validate_rpc()
        return self.replace(ModeArgument, ModeArgument("rpc"))
