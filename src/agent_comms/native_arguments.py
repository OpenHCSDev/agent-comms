"""Owned Pi launch options; unknown external arguments remain with Pi's parser."""

from __future__ import annotations

import os
import re
import shlex
from abc import abstractmethod
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from .coordination_errors import IdentityConflict
from .declared_family import DeclaredFamily
from .field_codec import FieldCodec
from .launch_runtime import LaunchRuntime
from .pi_vocabulary import (
    NoThinkingOverride,
    ThinkingChoice,
    ThinkingLevel,
)


@dataclass(frozen=True)
class NativeArgument(DeclaredFamily, affix="Argument"):
    @property
    @abstractmethod
    def argv(self) -> tuple[str, ...]: ...

    def validate_rpc(self) -> None:
        pass


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


class NamedOption(OptionArgument):
    @classmethod
    def flag(cls) -> str:
        return "--" + cls.declared_name.replace("_", "-")


class ValueOption(OptionArgument):
    @classmethod
    def consume(cls, inline: str | None, arguments: Iterator[str]) -> ValueOption:
        value = inline if inline is not None else next(arguments, None)
        if value is None:
            raise ValueError(f"{cls.flag()} requires a value")
        return cls.from_text(value)

    @classmethod
    @abstractmethod
    def from_text(cls, value: str) -> ValueOption: ...


@dataclass(frozen=True)
class ValueArgument(ValueOption):
    value: str

    @classmethod
    def from_text(cls, value: str) -> ValueArgument:
        return cls(value)

    @property
    def argv(self) -> tuple[str, ...]:
        return self.flag(), self.value


class ProviderArgument(NamedOption, ValueArgument):
    pass


class ModelArgument(NamedOption, ValueArgument):
    pass


class ExtensionArgument(NamedOption, ValueArgument):
    short = "-e"


@dataclass(frozen=True)
class SelectedModel:
    provider: str
    model: str

    def __post_init__(self) -> None:
        for value in (self.provider, self.model):
            FieldCodec.decode(str, value)
            if not value:
                raise IdentityConflict("Selected owner's configured provider/model is incomplete")
            if re.fullmatch(r"[^\s-]\S*", value) is None:
                raise IdentityConflict("Provider/model must be single non-option tokens")

    @classmethod
    def decode(cls, selection: str | None) -> SelectedModel:
        if not selection or "/" not in selection:
            raise IdentityConflict("Selected owner has no configured provider/model")
        provider, model = selection.split("/", 1)
        return cls(provider, model)

    @property
    def qualified(self) -> str:
        return f"{self.provider}/{self.model}"

    @property
    def arguments(self) -> tuple[NativeArgument, ...]:
        return ProviderArgument(self.provider), ModelArgument(self.model)

    @property
    def argv(self) -> tuple[str, ...]:
        return tuple(token for argument in self.arguments for token in argument.argv)


@dataclass(frozen=True)
class ThinkingArgument(NamedOption, ValueOption):
    level: type[ThinkingLevel]

    @classmethod
    def from_text(cls, value: str) -> ThinkingArgument:
        return cls(ThinkingLevel.member_named(value))

    @property
    def argv(self) -> tuple[str, ...]:
        return self.flag(), self.level.wire_value()


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
        return cls()

    @property
    def argv(self) -> tuple[str, ...]:
        return (self.flag(),)


class OneShotArgument(FlagArgument):
    def validate_rpc(self) -> None:
        raise ValueError("Managed Pi cannot run a one-shot CLI command")


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


class NoExtensionsArgument(NamedOption, FlagArgument):
    pass


@dataclass(frozen=True)
class NativeArguments:
    entries: tuple[NativeArgument, ...]
    binary_key: ClassVar[str] = "AGENT_COMMS_AGENT_BIN"
    arguments_key: ClassVar[str] = "AGENT_COMMS_AGENT_ARGS"
    default_binary: ClassVar[str] = "pi"

    @classmethod
    def owner_launcher(cls, runtime: LaunchRuntime) -> str:
        """The native launcher installed beside this owner's Python executable."""
        launcher = Path(runtime.native_entrypoint)
        if not launcher.is_file():
            raise ValueError("Owner runtime has no native Pi launcher; restart in a complete runtime")
        return str(launcher)

    @classmethod
    def configured_binary(
        cls, binary: str | None = None, environment: Mapping[str, str] | None = None,
    ) -> str:
        source = os.environ if environment is None else environment
        return binary or source.get(cls.binary_key, cls.default_binary)

    @classmethod
    def configured(
        cls,
        arguments: Sequence[str] | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> NativeArguments:
        if arguments is None:
            source = os.environ if environment is None else environment
            encoded = source.get(cls.arguments_key)
            arguments = () if encoded is None else shlex.split(encoded)
        return cls.parse(arguments)

    @classmethod
    def parse(cls, arguments: Sequence[str]) -> NativeArguments:
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

    def loads(self, extension: Path) -> bool:
        """Pi discovers installed extensions unless told not to; ``-e`` always loads its own."""
        return (NoExtensionsArgument() not in self.entries
                or ExtensionArgument(str(extension)) in self.entries)

    @property
    def model(self) -> str | None:
        selected = self.value(ModelArgument)
        provider = self.value(ProviderArgument)
        if selected is None or provider is None:
            return selected
        return SelectedModel(provider, selected.removeprefix(provider + "/")).qualified

    @property
    def thinking(self) -> type[ThinkingChoice]:
        return next((entry.level for entry in reversed(self.entries)
                     if isinstance(entry, ThinkingArgument)), NoThinkingOverride)

    def replace(self, member: type[NativeArgument], *entries: NativeArgument) -> NativeArguments:
        return NativeArguments(
            tuple(entry for entry in self.entries if not isinstance(entry, member)) + entries
        )

    def with_model(self, selected: str | None) -> NativeArguments:
        if selected is None or "/" not in selected:
            return self
        return self.replace(ProviderArgument).replace(
            ModelArgument, *SelectedModel.decode(selected).arguments
        )

    def with_thinking(self, selected: type[ThinkingChoice]) -> NativeArguments:
        return self.replace(ThinkingArgument, *selected.native_arguments(ThinkingArgument))

    def managed_rpc(self, package: Path) -> tuple[str, ...]:
        """Managed sessions load the protocol extensions; ephemeral RPC does not."""
        from .native_tools import NativeExecutableTool

        return (*NativeExecutableTool.extension_arguments(package), *self.rpc())

    def rpc(self) -> tuple[str, ...]:
        for entry in self.entries:
            entry.validate_rpc()
        return self.replace(ModeArgument, ModeArgument("rpc")).argv
