"""Owned Pi launch options; unknown external arguments remain with Pi's parser."""

from __future__ import annotations

import logging
import os
import re
import shlex
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from functools import partial, reduce
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from agent_comms.coordination.errors import IdentityConflict
from agent_comms.execution.launch_runtime import LaunchRuntime
from agent_comms.execution.pi_vocabulary import NoThinkingOverride, ThinkingChoice, ThinkingLevel
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import FieldCodec, NullMember, SingleFieldValue, SubclassUnion

if TYPE_CHECKING:
    from agent_comms.execution.owner_lifecycle import OwnerLifecycle
    from agent_comms.execution.channel_coding_tools import CodingToolOwner
    from agent_comms.team.rule_deal import UndealtRecord
    from agent_comms.transport.native_tool_transport import NativeToolMode
    from agent_comms.registry.thread_model import ThreadModel

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NativeArgument(DeclaredFamily, affix="Argument"):
    @property
    @abstractmethod
    def argv(self) -> tuple[str, ...]: ...

    def validate_rpc(self) -> None:
        pass

    def apply_thinking(self, current: type[ThinkingChoice]) -> type[ThinkingChoice]:
        return current

    def apply_provider(self, current: ProviderChoice) -> ProviderChoice:
        return current

    def apply_model(self, current: ThreadModel) -> ThreadModel:
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
    shorts: ClassVar[tuple[str, ...]] = ()

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


class ProviderChoice(ABC):
    """The provider the arguments select, or none; it qualifies a model name."""

    @abstractmethod
    def qualified(self, model: str) -> str: ...

    @abstractmethod
    def provider_type(self) -> type[ModelProvider]: ...


@dataclass(frozen=True)
class NoProviderChoice(ProviderChoice):
    def qualified(self, model: str) -> str:
        return model

    def provider_type(self) -> type[ModelProvider]:
        return NoSelectedModel


class ProviderArgument(NamedOption, ValueArgument, ProviderChoice):
    def apply_provider(self, current: ProviderChoice) -> ProviderChoice:
        return self

    def qualified(self, model: str) -> str:
        return SelectedModel(self.value, model.removeprefix(self.value + "/")).qualified

    def provider_type(self) -> type[ModelProvider]:
        return ModelProvider.configured(self.value)

    def kept_on_model_change(self) -> tuple[NativeArgument, ...]:
        return ()


class ModelArgument(NamedOption, ValueArgument):
    def apply_model(self, current: ThreadModel) -> ThreadModel:
        return current.declared(self.value)

    def kept_on_model_change(self) -> tuple[NativeArgument, ...]:
        return ()


class ExtensionArgument(NamedOption, ValueArgument):
    shorts = ("-e",)


class ModelProvider(DeclaredFamily, affix="Provider"):
    """Provider declarations own the native extensions and the action space their models
    require."""

    @classmethod
    def configured(cls, name: str) -> type[ModelProvider]:
        """The provider Pi's model configuration calls `name`: its declaration here, or the
        one member for every provider nothing here specializes. Provider names are Pi's to
        add, so an undeclared one is a provider, not an error."""
        return cls.member_or_default(name, ConfiguredProvider)

    @classmethod
    def extension_arguments(cls, package: Path) -> tuple[str, ...]:
        return ()

    @classmethod
    def extension_environment(cls) -> Mapping[str, str]:
        return {}

    @classmethod
    def native_tool_mode(cls, mode: NativeToolMode, owner: CodingToolOwner) -> NativeToolMode:
        """The tools a model of this provider runs with for an input granted `mode`, whose
        coding calls `owner` authorizes."""
        return mode

    @classmethod
    def undealt(cls, record: UndealtRecord) -> UndealtRecord:
        """The rule-gate settings a never-dealt thread on this provider starts from."""
        return record

    @classmethod
    @asynccontextmanager
    async def turn(cls, session_file: str | Path | None) -> AsyncIterator[None]:
        """Around one turn on this provider, whichever path runs it: its start before the
        first request, its end however the turn finished."""
        from agent_comms.coordination.coordinator import Coordination

        await Coordination.run_worker(partial(cls.turn_starting, session_file))
        try:
            yield
        finally:
            await Coordination.run_worker(partial(cls.turn_ended, session_file))

    @classmethod
    def turn_starting(cls, session_file: str | Path | None) -> None:
        """Before a turn on this provider sends its first request."""

    @classmethod
    def turn_ended(cls, session_file: str | Path | None) -> None:
        """After a turn on this provider finished, however it ended."""


class NoSelectedModel(ModelProvider, declared_name=None):
    """A thread with no native model selected."""


class ConfiguredProvider(ModelProvider):
    """Every provider that Pi's own model configuration names and nothing here specializes;
    it runs as the base does."""


class AnthropicProvider(ModelProvider, declared_name="anthropic"):
    """Declared by name."""


class OpenAIProvider(ModelProvider, declared_name="openai"):
    """Declared by name."""


class OpenAICodexProvider(ModelProvider, declared_name="openai-codex"):
    """Declared by name."""


class ClmLocalProvider(ModelProvider, declared_name="clm-local"):
    """Local context language models, served under the reference harness they were trained in:
    clm_reference.mjs mirrors the context to a file the model edits with bash, with the
    reference's 28K budget and 2,048-token reserve (the RL arm's configuration, paper App. E)."""

    budget_tokens: ClassVar[int] = 28_672
    reserve_tokens: ClassVar[int] = 2_048
    # When the model must compact, by the rule's declared name in clm_reference.mjs (EditRule).
    # "budget" is the reference harness: only budget reminders press. Under it iter44 edited
    # twice in sixteen turns, both after a reminder (CLM-tristan, 2026-11-20), and cannot read a
    # document larger than its window; "digest" makes each sizeable command output's notes and
    # removal the precondition of the next command (Tristan, 2026-11-20: "can't we FORCE it").
    edit_rule: ClassVar[str] = "digest"

    @classmethod
    def extension_arguments(cls, package: Path) -> tuple[str, ...]:
        return ("-e", str(package / "dist/clm_reference.mjs"))

    @classmethod
    def extension_environment(cls) -> Mapping[str, str]:
        return {
            "CLM_CONTEXT_BUDGET": str(cls.budget_tokens),
            "CLM_CONTEXT_RESERVE": str(cls.reserve_tokens),
            "CLM_EDIT_RULE": cls.edit_rule,
        }

    @classmethod
    def native_tool_mode(cls, mode: NativeToolMode, owner: CodingToolOwner) -> NativeToolMode:
        from agent_comms.execution.context_language_model import ContextLanguageModelMode

        return ContextLanguageModelMode(owner)

    @classmethod
    def undealt(cls, record: UndealtRecord) -> UndealtRecord:
        """No papers, rules or audit by default (Tristan, 2026-11-20): the deal is context the
        checkpoint never saw and its window cannot hold, and its audit is not its protocol."""
        from agent_comms.team.rule_deal import OffDealMode

        return replace(record, mode=OffDealMode)

    # The conversations' recorded contexts and the slot's holder: durable state, outside the
    # bus root, which is carried between bus versions by its own roster.
    state_directory: ClassVar[Path] = Path("~/.local/state/agent-comms/clm")
    # The llama-server unit's --slot-save-path: the server does not report it.
    slot_directory: ClassVar[Path] = Path("~/.cache/agent-comms/llama-slots")

    @classmethod
    def context_state(cls):
        from agent_comms.execution import context_state
        from agent_comms.execution.launch_runtime import RestartEnvironment

        return context_state.ContextStateStore(
            cls.state_directory.expanduser(),
            context_state.SlotServer.configured(
                RestartEnvironment.default_agent_directory, cls.declared_name),
            cls.slot_directory.expanduser(),
        )

    @classmethod
    def turn_starting(cls, session_file: str | Path | None) -> None:
        """Take the one llama-server slot for this conversation: the previous holder's state
        is saved, and this conversation's is restored when it still matches its context."""
        from agent_comms.execution import context_state

        session = context_state.conversation(session_file)
        if session is not None:
            outcome = context_state.recoverable(cls.context_state().enter, session)
            logger.info("CLM slot for %s: %s", session, outcome)

    @classmethod
    def turn_ended(cls, session_file: str | Path | None) -> None:
        """Commit the context this conversation's harness last sent to its history."""
        from agent_comms.execution import context_state

        session = context_state.conversation(session_file)
        if session is not None:
            context_state.recoverable(cls.context_state().history(session).record)


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
        return ModelProvider.configured(self.provider)

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
    shorts = ("-c",)


class ResumeArgument(SessionSelectionArgument, NamedOption, OneShotArgument):
    shorts = ("-r",)


class NoSessionArgument(SessionSelectionArgument, NamedOption, OneShotArgument):
    pass


class PrintArgument(NamedOption, OneShotArgument):
    shorts = ("-p",)


class HelpArgument(NamedOption, OneShotArgument):
    shorts = ("-h",)


class VersionArgument(NamedOption, OneShotArgument):
    shorts = ("-v",)


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
            for flag in (member.flag(), *member.shorts)
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
        return self.provider.provider_type()

    def last[T](self, apply: Callable[[NativeArgument], Callable[[T], T]]) -> Callable[[T], T]:
        """Pi lets the last argument of a setting win: each entry's apply hook owns its part."""
        return lambda start: reduce(
            lambda selected, entry: apply(entry)(selected), self.entries, start)

    @property
    def provider(self) -> ProviderChoice:
        return self.last(lambda entry: entry.apply_provider)(NoProviderChoice())

    @property
    def thinking(self) -> type[ThinkingChoice]:
        return self.last(lambda entry: entry.apply_thinking)(NoThinkingOverride)

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

    def managed_environment(
        self, environment: Mapping[str, str] | None,
    ) -> dict[str, str]:
        return {
            **(environment or {}),
            **self.provider_type.extension_environment(),
        }

    def managed_rpc(self, package: Path) -> tuple[str, ...]:
        """Managed sessions load only the packaged extensions, never discovered copies of them."""
        from agent_comms.transport.native_tools import NativeToolDeclaration

        return (
            "--no-extensions",
            *NativeToolDeclaration.extension_arguments(package),
            *self.provider_type.extension_arguments(package),
            *self.rpc(),
        )

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
