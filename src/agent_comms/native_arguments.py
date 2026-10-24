"""Owned Pi launch options; unknown external arguments remain with Pi's parser."""

from __future__ import annotations

import os
import re
import shlex
from abc import abstractmethod
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from functools import reduce
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from .coordination_errors import IdentityConflict
from .declared_family import DeclaredFamily
from .field_codec import FieldCodec, NullMember, SingleFieldValue, SubclassUnion
from .launch_runtime import LaunchRuntime

if TYPE_CHECKING:
    from .owner_lifecycle import OwnerLifecycle
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

    def apply_thinking(self, current: type[ThinkingChoice]) -> type[ThinkingChoice]:
        return current

    def apply_provider(self, current: str | None) -> str | None:
        return current

    def apply_model(self, current: str | None) -> str | None:
        return current

    def kept_on_model_change(self) -> tuple[NativeArgument, ...]:
        return (self,)

    def kept_on_thinking_change(self) -> tuple[NativeArgument, ...]:
        return (self,)

    def kept_on_rpc(self) -> tuple[NativeArgument, ...]:
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
    def apply_provider(self, current: str | None) -> str | None:
        return self.value

    def kept_on_model_change(self) -> tuple[NativeArgument, ...]:
        return ()


class ModelArgument(NamedOption, ValueArgument):
    def apply_model(self, current: str | None) -> str | None:
        return self.value

    def kept_on_model_change(self) -> tuple[NativeArgument, ...]:
        return ()


class ExtensionArgument(NamedOption, ValueArgument):
    short = "-e"


class ModelProvider(DeclaredFamily, affix="Provider"):
    """Provider declarations own command permission; unknown names are refused."""

    may_command: ClassVar[bool]


class NoSelectedModel(ModelProvider, declared_name=None):
    """External commanders without a native model remain valid commanders."""
    may_command = True


class AnthropicProvider(ModelProvider, declared_name="anthropic"):
    may_command = True


class OpenAIProvider(ModelProvider, declared_name="openai"):
    may_command = True


class OpenAICodexProvider(ModelProvider, declared_name="openai-codex"):
    may_command = False


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
    def provider_type(self) -> type[ModelProvider]:
        return ModelProvider.member_named(self.provider)

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

    def apply_thinking(self, current: type[ThinkingChoice]) -> type[ThinkingChoice]:
        return self.level

    def kept_on_thinking_change(self) -> tuple[NativeArgument, ...]:
        return ()

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


class NativeInputProofArgument(SessionSelectionArgument, NamedOption, ValueArgument):
    """Only the captured bus owner supplies native proof storage."""


class SessionIdArgument(SessionSelectionArgument, NamedOption, ValueArgument):
    pass


class ForkArgument(SessionSelectionArgument, NamedOption, ValueArgument):
    pass


class ModeArgument(NamedOption, ValueArgument):
    def validate_rpc(self) -> None:
        if self.value != "rpc":
            raise ValueError("Managed Pi requires RPC mode")

    def kept_on_rpc(self) -> tuple[NativeArgument, ...]:
        return ()


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

    def loads(self, extension: Path) -> bool:
        """Pi discovers installed extensions unless told not to; ``-e`` always loads its own."""
        return (NoExtensionsArgument() not in self.entries
                or ExtensionArgument(str(extension)) in self.entries)

    @property
    def provider_type(self) -> type[ModelProvider]:
        provider = self.provider
        return NoSelectedModel if provider is None else ModelProvider.member_named(provider)

    def last[T](self, start: T, apply: Callable[[NativeArgument], Callable[[T], T]]) -> T:
        """Pi lets the last argument of a setting win: each entry's apply hook owns its part."""
        return reduce(lambda selected, entry: apply(entry)(selected), self.entries, start)

    @property
    def provider(self) -> str | None:
        return self.last(None, lambda entry: entry.apply_provider)

    @property
    def model(self) -> str | None:
        return self.last(None, lambda entry: entry.apply_model)

    @property
    def thinking(self) -> type[ThinkingChoice]:
        return self.last(NoThinkingOverride, lambda entry: entry.apply_thinking)

    def with_model(self, selected: str) -> NativeArguments:
        return NativeArguments(
            tuple(kept for entry in self.entries for kept in entry.kept_on_model_change())
            + SelectedModel.decode(selected).arguments
        )

    def with_thinking(self, selected: type[ThinkingChoice]) -> NativeArguments:
        return NativeArguments(
            tuple(kept for entry in self.entries for kept in entry.kept_on_thinking_change())
            + selected.native_arguments(ThinkingArgument)
        )

    def managed_rpc(self, package: Path) -> tuple[str, ...]:
        """Managed sessions load only the packaged extensions, never discovered copies of them."""
        from .native_tools import NativeToolDeclaration

        return ("--no-extensions", *NativeToolDeclaration.extension_arguments(package), *self.rpc())

    def rpc(self) -> tuple[str, ...]:
        for entry in self.entries:
            entry.validate_rpc()
        return NativeArguments(
            tuple(kept for entry in self.entries for kept in entry.kept_on_rpc())
            + (ModeArgument("rpc"),)
        ).argv


class LaunchBinary(SubclassUnion, affix="Binary"):
    """The native binary an owner launches: the operator's own, or the configured one."""

    @abstractmethod
    def configured(self, environment: Mapping[str, str] = os.environ) -> str: ...

    @abstractmethod
    def restarted(self, lifecycle: OwnerLifecycle, source_binary: str, runtime: LaunchRuntime) -> str: ...


@dataclass(frozen=True, slots=True)
class GivenBinary(SingleFieldValue, LaunchBinary):
    path: str

    def configured(self, environment: Mapping[str, str] = os.environ) -> str:
        return self.path

    def restarted(self, lifecycle: OwnerLifecycle, source_binary: str, runtime: LaunchRuntime) -> str:
        return self.path


@dataclass(frozen=True, slots=True)
class ConfiguredBinary(NullMember, LaunchBinary):
    def configured(self, environment: Mapping[str, str] = os.environ) -> str:
        return environment.get(NativeArguments.binary_key, NativeArguments.default_binary)

    def restarted(self, lifecycle: OwnerLifecycle, source_binary: str, runtime: LaunchRuntime) -> str:
        return lifecycle.restart_entrypoint(source_binary, runtime)


class LaunchArguments(SubclassUnion, affix="Arguments"):
    """The native arguments an owner launches with: the operator's own, or inherited."""

    @abstractmethod
    def configured(self, environment: Mapping[str, str] = os.environ) -> NativeArguments: ...

    @abstractmethod
    def restarted(self, source_arguments: Sequence[str]) -> GivenArguments: ...

    @abstractmethod
    def apply(self, environment: dict[str, str]) -> None: ...


@dataclass(frozen=True, slots=True)
class GivenArguments(SingleFieldValue, LaunchArguments):
    argv: tuple[str, ...]

    def configured(self, environment: Mapping[str, str] = os.environ) -> NativeArguments:
        return NativeArguments.parse(self.argv)

    def restarted(self, source_arguments: Sequence[str]) -> GivenArguments:
        return self

    def apply(self, environment: dict[str, str]) -> None:
        environment[NativeArguments.arguments_key] = shlex.join(self.argv)


@dataclass(frozen=True, slots=True)
class InheritedArguments(NullMember, LaunchArguments):
    def configured(self, environment: Mapping[str, str] = os.environ) -> NativeArguments:
        return NativeArguments.parse(shlex.split(environment.get(NativeArguments.arguments_key, "")))

    def restarted(self, source_arguments: Sequence[str]) -> GivenArguments:
        return GivenArguments(tuple(source_arguments))

    def apply(self, environment: dict[str, str]) -> None:
        """The launch inherits the arguments its environment already carries."""
