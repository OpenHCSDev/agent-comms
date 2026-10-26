"""Declaration-owned CLI operation and its required dependencies."""

from __future__ import annotations

from agent_comms.channel_targets import NoChannel, SurroundingChannel

from abc import ABC, abstractmethod
from agent_comms.active_route import ActiveRoute
from agent_comms.catalog_document import CatalogAccess, NoCatalog
from agent_comms.channel_targets import ChannelSelection, CommandTarget, NoChannel
from agent_comms.cli_failures import CLI_FAILURES
from agent_comms.cli_result import CliResult, ResultShower
from agent_comms.command import Command
from agent_comms.comms import Comms
from agent_comms.config_options import ConfigOptions
from agent_comms.errors import ToolRefusedError
from agent_comms.field_codec import FieldCodec, RecordMember, WireValue
from agent_comms.installer.owner_starts import OwnerStart, RefusedOwner, StartedOwner
from agent_comms.lifecycle_capability import LifecycleCapability, Successful
from agent_comms.registry_document import RegistrySnapshot
from agent_comms.target_fields import TargetField
from dataclasses import dataclass, replace
from pydantic import JsonValue
from typing import Annotated, Self

from agent_comms.channel_targets import OneChannel, ResolvedChannel, ResolvedThread
from agent_comms.declared_verb import DeclaredCommand
from agent_comms.declared_family import UnknownFamilyMember
from metaclass_registry.discovery import import_module_preserving_root_logging
from agent_comms.field_codec import FieldPlan, TargetBound
from agent_comms.launch_root import LaunchRoot
from inspect import iscoroutinefunction, signature
from types import get_original_bases
from typing import TYPE_CHECKING, TextIO, cast, get_args, get_origin
import os

if TYPE_CHECKING:
    from agent_comms.active_route import CommsRoute
    from agent_comms.thread_status import ThreadStatus
    from agent_comms.threads import Thread


class ChannelBound(TargetBound):
    """A target-bound field supplies the surrounding channel scope."""


class CliExecution[Answer: CliResult = CliResult](Command["Comms", Answer]):
    """The dispatcher contract, including methods selected through multiple inheritance."""

    @abstractmethod
    def execute(self, route: CommsRoute, output: TextIO) -> int:
        """Execute against the selected route and write to the caller's stream."""

    def __init_subclass__(cls) -> None:
        super().__init_subclass__()
        expected, actual = (
            (iscoroutinefunction(method),
             tuple((parameter.kind, parameter.default)
                   for parameter in signature(method, follow_wrapped=False).parameters.values()))
            for method in (CliExecution.execute, cls.execute)
        )
        if actual != expected:
            raise TypeError(f"{cls.__qualname__}.execute{signature(cls.execute)} disagrees with "
                            f"CliExecution.execute{signature(CliExecution.execute)}")


@dataclass(frozen=True, kw_only=True)
class CliCommand[Answer: CliResult = CliResult](
    DeclaredCommand,
    CliExecution[Answer],
    affix="CliCommand",
    discovery_package="agent_comms.cli_verbs",
):
    @staticmethod
    def module_stem(name: str) -> str:
        """The CLI spelling and its importable Python module share one derivation."""
        return name.replace("-", "_")

    @classmethod
    def validate_module_name(cls) -> None:
        actual = cls.__module__.rpartition(".")[2]
        expected = cls.module_stem(cls.declared_name)
        if actual != expected:
            raise TypeError(f"{cls.__qualname__} declares {cls.declared_name!r}; "
                            f"its module stem must be {expected!r}, not {actual!r}")

    @classmethod
    def _prepare_member(cls, name: object) -> None:
        selected = FieldCodec.decode(str, name)
        package = FieldCodec.decode(str, cls._family_root.__registry_config__.discovery_package)
        module = f"{package}.{cls.module_stem(selected)}"
        try:
            import_module_preserving_root_logging(module)
        except ModuleNotFoundError as error:
            if error.name != module:
                raise
            raise UnknownFamilyMember(f"Unknown {cls.__name__} name: {name!r}") from error

    def schedule(self, batch: TargetBatchSchedule, comms: Comms) -> None:
        result = TargetCompleted(batch.target, self, self.apply(comms))
        batch.outcomes.append((batch.position, result))

    @classmethod
    def catalog_declaration(cls) -> type[CliCommand]:
        """Concrete thread/channel variants declare their shared menu operation."""
        return cls

    @classmethod
    def result_type(cls) -> type[Answer]:
        """The result this command declares once, as the family's type argument in its own or
        an inherited class header."""
        for declaration in cls.__mro__:
            for base in get_original_bases(declaration):
                origin, arguments = get_origin(base), get_args(base)
                if isinstance(origin, type) and issubclass(origin, CliCommand) and arguments:
                    (answer,) = arguments
                    return cast(type[Answer], answer)
        raise TypeError(f"{cls.__qualname__} declares no result in its class header")

    @classmethod
    def invocation(cls, *arguments: str) -> tuple[str, ...]:
        """This command's invocation on the agent-comms command line."""
        return (cls.declared_name, *arguments)

    @classmethod
    def selected_targets(cls, targets: tuple[CommandTarget, ...]) -> TargetSelection:
        """A single-target command runs on one target; a wider selection is a batch its
        run refuses (``require_batch``) and the menu catalog lists batch actions for."""

        distinct = tuple(dict.fromkeys(targets))
        return (OneTarget if len(distinct) == 1 else TargetBatch)(distinct)

    @classmethod
    def require_batch(cls) -> None:
        raise ValueError('This action requires a single target')

    @classmethod
    def channel_fields(cls) -> tuple[FieldPlan, ...]:
        """The declared target-bound channel parameters own membership scope."""
        return tuple(
            entry
            for entry in FieldCodec.argument_fields(cls)
            if entry.setting(ChannelBound, False)
        )

    @classmethod
    def selection_channels(cls, channel: ChannelSelection,
                           target: str) -> tuple[SurroundingChannel, ...]:
        return channel.channels_for(target)

    @classmethod
    def thread_bindings(cls, comms: Comms, thread: Thread, status: ThreadStatus, channel: SurroundingChannel = NoChannel(), *, catalog: CatalogAccess = NoCatalog()) -> tuple[Self, ...]:
        return ()

    @classmethod
    def project_bindings(cls, comms: Comms, project: str) -> tuple[Self, ...]:
        return ()

    @classmethod
    def channel_bindings(cls, comms: Comms, resolved: ResolvedChannel, *, snapshot: RegistrySnapshot, catalog: CatalogAccess) -> tuple[Self, ...]:
        return ()

    @classmethod
    def bindings(cls, comms: Comms, target: CommandTarget, channel: SurroundingChannel = NoChannel(),
                 *, snapshot: RegistrySnapshot, catalog: CatalogAccess,
                 resolved: ResolvedThread | ResolvedChannel | str) -> tuple[Self, ...]:
        return target.bindings(
            cls, comms, channel, resolved, snapshot=snapshot, catalog=catalog)

    @classmethod
    def target_catalog(cls, comms: Comms, target: TargetSelection,
                       channel: ChannelSelection = NoChannel(),
                       *, project: str) -> tuple[TargetAction, ...]:

        selected = target.targets
        # A catalog is one observation. Declarations borrow its original store
        # resources; execution still acquires and rebinds current state.
        snapshot = comms.registry.snapshot()
        # View actors (including an implicit project actor) need not declare a
        # thread. Discovery has no thread actions for absent members; execution
        # still requires its original current binding. Resolve aliases through
        # the acquired namespace rather than reserving a special actor spelling.
        available_targets = tuple(item for item in selected if item.available(snapshot))
        catalog = (comms.channels.catalog.read()
                   if channel.needs_catalog or any(item.needs_channel_state for item in selected) else NoCatalog())
        resolved_targets = tuple((name, name.resolve(snapshot, comms.bus.require_channel)) for name in available_targets)
        grouped: dict[type[CliCommand], dict[CliCommand, None]] = {}
        for member in target.command_members(cls):
            selected_channels = channel if member.channel_fields() else NoChannel()
            for name, resolved in resolved_targets:
                for context in name.contexts(member, selected_channels):
                    available = member.bindings(
                        comms, name, context, snapshot=snapshot, catalog=catalog, resolved=resolved)
                    if available:
                        declaration = target.listed_declaration(member)
                        grouped.setdefault(declaration, {}).update(dict.fromkeys(
                            command.for_editor(comms, name.name, project) for command in available))
        names = tuple(name for item in selected for name in item.destination_names)
        return tuple(TargetAction(tuple(commands), names, declaration,
                                  channel if declaration.channel_fields() else NoChannel())
                     for declaration, commands in grouped.items())

    def for_editor(self, comms: Comms, target: str, project: str) -> Self:
        return self

    @property
    def editable_fields(self) -> tuple[TargetField, ...]:
        """The command owns the parameters offered by its target action."""

        return tuple(
            TargetField.of(entry, entry.current(self))
            for entry in FieldCodec.argument_fields(type(self))
            if not entry.setting(TargetBound, False)
        )

    async def prepare_fields(self) -> tuple[TargetField, ...]:

        catalogs = ConfigOptions.configured()
        return tuple([await item.prepare(catalogs, self) for item in self.editable_fields])

    def describe(self, comms: Comms, target: CommandTarget, project: str) -> TargetAction:

        bound = self.for_editor(comms, target.name, project)
        channels = next((OneChannel(getattr(bound, item.name))
                         for item in bound.channel_fields()), NoChannel())
        return TargetAction((bound,), target.destination_names, type(bound), channels)

    def run(self, ctx: Comms, output: TextIO) -> int:
        """Run this command and write its declared result."""
        return self.apply(ctx).write(output)

    @classmethod
    def published_launch(cls) -> LaunchRoot:
        """The root a command reads when no --root is given."""

        return ActiveRoute.published_launch()

    def execute(self, route: CommsRoute, output: TextIO) -> int:
        """Open the bus this command reads, then run it; a command that must act before
        the bus opens overrides this."""

        with Comms.client(route) as comms:
            return self.run(comms, output)

    @classmethod
    def editor_arguments(cls, values: dict[str, str | None]) -> dict[str, object]:
        return FieldCodec.editor_arguments(cls, values)

    def confirmation(self) -> str:
        return ''

    def selection_confirmation(self, commands: tuple[CliCommand, ...]) -> str:
        return '\n'.join(dict.fromkeys(warning for command in commands
                                      if (warning := command.confirmation())))

    def with_confirmation(self, confirmed: bool) -> Self:
        if self.confirmation() and not confirmed:
            raise ValueError(self.confirmation())
        return self

    def edited(self, arguments: dict[str, object]) -> Self:
        bound = {
            entry.key
            for entry in FieldCodec.argument_fields(type(self))
            if entry.setting(TargetBound, False)
        }
        if bound & arguments.keys():
            raise ValueError("Target-bound parameters cannot be overridden")
        editable = {item.name for item in self.editable_fields}
        if arguments.keys() - editable:
            raise ValueError("Parameters not offered by this target command cannot be overridden")
        encoded = FieldCodec.encode(self)
        return type(self).from_payload(
            {
                **{key: encoded[key] for key in bound & encoded.keys()},
                type(self).family_discriminator: FieldCodec.encode(type(self)),
                **arguments,
            }
        )

    @classmethod
    def execute_target(cls, comms: Comms, target: TargetSelection, arguments: dict[str, object],
                       *, confirmed: bool = False,
                       channel: ChannelSelection = NoChannel(),
                       project: str | None = None) -> CliResult:
        if not cls.channel_fields():
            channel.require_unbound()
            channel = NoChannel()
        snapshot = comms.registry.snapshot()
        catalog = (comms.channels.catalog.read()
                   if channel.needs_catalog or any(item.needs_channel_state for item in target.targets) else NoCatalog())
        return target.run(cls, comms, arguments, confirmed=confirmed, channel=channel,
                             project=os.getcwd() if project is None else project,
                             snapshot=snapshot, catalog=catalog)


@dataclass(frozen=True)
class TargetAction:
    """A selection keeps the bound commands, not a separate permission list."""
    bound: tuple[CliCommand, ...]
    targets: tuple[str, ...]
    declaration: type[CliCommand]
    channels: ChannelSelection = NoChannel()

    @property
    def editable_fields(self) -> tuple[TargetField, ...]:
        return self.bound[0].editable_fields

    async def prepare_fields(self) -> tuple[TargetField, ...]:
        return await self.bound[0].prepare_fields()

    @property
    def label(self) -> str:
        return self.declaration.help

    @property
    def confirmation(self) -> str:
        groups: dict[type[CliCommand], list[CliCommand]] = {}
        for command in self.bound:
            groups.setdefault(type(command), []).append(command)
        return '\n'.join(filter(None, (commands[0].selection_confirmation(tuple(commands))
                                       for commands in groups.values())))

    def edited(self, arguments: dict[str, str | None]) -> TargetAction:
        return replace(self, bound=tuple(command.edited(type(command).editor_arguments(arguments))
                                        for command in self.bound))

    def with_confirmation(self, confirmed: bool) -> TargetAction:
        return replace(self, bound=tuple(command.with_confirmation(confirmed)
                                        for command in self.bound))

    def encode(self) -> dict[str, object]:
        """Only the CLI boundary requests the external catalog JSON shape."""
        return {'command': FieldCodec.encode(self.declaration), 'label': self.label,
                'parameters': {'type': 'object', 'additionalProperties': False,
                    'properties': {item.name: item.json_schema() for item in self.editable_fields},
                    'required': [item.name for item in self.editable_fields if item.required]},
                'confirmation': self.confirmation, 'targets': list(self.targets),
                'bound_count': len(self.bound)}


class TargetOutcome(LifecycleCapability, ABC):
    """One attempted selection member; failure never claims absence of effects."""

    @abstractmethod
    def show(self, shower: ResultShower, label: str, title: str) -> None: ...

    @abstractmethod
    def owner_start(self) -> OwnerStart: ...

    def reconnect_targets(self) -> tuple[str, ...]:
        return ()

    def open_targets(self) -> tuple[str, ...]:
        return ()


@dataclass(frozen=True)
class TargetCompleted(Successful, TargetOutcome, RecordMember):
    target: CommandTarget
    command: CliCommand
    result: Annotated[CliResult, WireValue]

    def show(self, shower: ResultShower, label: str, title: str) -> None:
        shower.show_done(f"{label}: {self.target.display_name()}", title)

    def owner_start(self) -> OwnerStart:
        return StartedOwner(self.target.display_name())

    @classmethod
    def from_wire(cls, data: JsonValue) -> Self:
        record = FieldCodec.decode(dict[str, JsonValue], data)
        command = FieldCodec.decode(CliCommand, record["command"])
        return FieldCodec.decode_record(cls, record, field_types={"result": command.result_type()})

    def reconnect_targets(self) -> tuple[str, ...]:
        return self.result.reconnect_targets()

    def open_targets(self) -> tuple[str, ...]:
        return self.result.open_targets()


@dataclass(frozen=True)
class TargetFailed(TargetOutcome):
    target: CommandTarget
    error_type: str
    error: str

    def __str__(self) -> str:
        return f"{self.target.display_name()}: {self.error_type}: {self.error}"

    def show(self, shower: ResultShower, label: str, title: str) -> None:
        shower.show_error(str(self), title)

    def owner_start(self) -> OwnerStart:
        return RefusedOwner(self.target.display_name(), str(self))


@dataclass(frozen=True)
class TargetBatchResult(CliResult):
    outcomes: tuple[TargetCompleted | TargetFailed, ...]

    def show(self, shower: ResultShower, label: str, title: str) -> None:
        for outcome in self.outcomes:
            outcome.show(shower, label, title)

    @property
    def completed_count(self) -> int:
        return sum(len(outcome.items_with(Successful, outcome)) for outcome in self.outcomes)

    @property
    def successful(self) -> bool:
        return self.completed_count == len(self.outcomes)

    def reconnect_targets(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(name for outcome in self.outcomes
                                   for name in outcome.reconnect_targets()))

    def open_targets(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(name for outcome in self.outcomes
                                   for name in outcome.open_targets()))

    def exit_code(self) -> int:
        return int(not self.successful)

    def require_success(self, refusal: type[Exception]) -> None:
        for outcome in self.outcomes:
            outcome.require(Successful, refusal(str(outcome)))


class CommandSelectionChanged(ToolRefusedError):
    """The selected command is no longer bound to the captured target."""


@dataclass(frozen=True)
class TargetSelection(ABC):
    """The targets one execution runs on; each kind owns how its result comes back."""
    targets: tuple[CommandTarget, ...]

    def __post_init__(self) -> None:
        if not self.targets:
            raise ValueError('Select at least one target')

    def command_members(self, family: type[CliCommand]) -> tuple[type[CliCommand], ...]:
        """Menu membership on one target: every member of the family."""
        return family.members_with(family)

    def listed_declaration(self, member: type[CliCommand]) -> type[CliCommand]:
        """The declaration a catalog lists for a member bound on this selection."""
        return member

    @abstractmethod
    def run(self, declaration: type[CliCommand], comms: Comms, arguments: dict[str, object],
            *, confirmed: bool, channel: ChannelSelection, project: str,
            snapshot: RegistrySnapshot, catalog: CatalogAccess) -> CliResult:
        """Bind, authorize and apply the declaration on this selection."""


class OneTarget(TargetSelection):
    """A single target: resolve errors raise and the bound command's own result returns."""

    def __post_init__(self) -> None:
        super().__post_init__()
        if len(self.targets) != 1:
            raise ValueError('This action requires a single target')

    def run(self, declaration: type[CliCommand], comms: Comms, arguments: dict[str, object], *, confirmed: bool, channel: ChannelSelection, project: str, snapshot: RegistrySnapshot, catalog: CatalogAccess) -> CliResult:
        name, = self.targets
        resolved = name.resolve(snapshot, comms.bus.require_channel)
        bindings = tuple(bound for context in name.contexts(declaration, channel)
                         for bound in declaration.bindings(comms, name, context, snapshot=snapshot,
                                                           catalog=catalog, resolved=resolved))
        if len(bindings) == 1:
            bound = bindings[0].for_editor(comms, name.name, project).edited(arguments)
            return bound.with_confirmation(confirmed).apply(comms)
        if not bindings:
            raise ValueError('This action is no longer available for the target')
        # Several bindings on one target run as a batch of them.
        return TargetBatch(self.targets).run(declaration, comms, arguments, confirmed=confirmed,
                                             channel=channel, project=project,
                                             snapshot=snapshot, catalog=catalog)


@dataclass(frozen=True)
class TargetBatchSchedule:
    """One bound command's position and the batch collections it schedules into."""

    position: int
    target: CommandTarget
    outcomes: list[tuple[int, TargetCompleted | TargetFailed]]
    together: dict[type[AppliedTogether], list[tuple[int, CommandTarget, AppliedTogether]]]


class TargetBatch(TargetSelection):
    """Every other selection: each target's failure is collected and a TargetBatchResult returns."""

    def command_members(self, family: type[CliCommand]) -> tuple[type[CliCommand], ...]:
        return family.members_with(MultipleTargets)

    def listed_declaration(self, member: type[CliCommand]) -> type[CliCommand]:
        return member.catalog_declaration()

    def run(self, declaration: type[CliCommand], comms: Comms, arguments: dict[str, object], *, confirmed: bool, channel: ChannelSelection, project: str, snapshot: RegistrySnapshot, catalog: CatalogAccess) -> CliResult:
        declaration.require_batch()
        declarations = (tuple(member for member in CliCommand.members_with(CliCommand)
                              if member.catalog_declaration() is declaration)
                        if declaration.catalog_declaration() is declaration else (declaration,))
        outcomes: list[tuple[int, TargetCompleted | TargetFailed]] = []
        planned: dict[CliCommand, tuple[int, CommandTarget, SurroundingChannel]] = {}
        together: dict[type[AppliedTogether], list[tuple[int, CommandTarget, AppliedTogether]]] = {}
        for name in self.targets:
            try:
                resolved = name.resolve(snapshot, comms.bus.require_channel)
            except ToolRefusedError as error:
                outcomes.append((len(planned) + len(outcomes),
                                 TargetFailed(name, type(error).__name__, str(error))))
                continue
            for context in name.contexts(declaration, channel):
                try:
                    bindings = tuple(command for member in declarations
                                     for command in member.bindings(comms, name, context,
                                                                   snapshot=snapshot, catalog=catalog, resolved=resolved))
                    if not bindings:
                        scope = f' in {context}' if context is not None else ''
                        raise CommandSelectionChanged(f'This action is no longer available for {name}{scope}')
                except ToolRefusedError as error:
                    outcomes.append((len(planned) + len(outcomes),
                                     TargetFailed(name, type(error).__name__, str(error))))
                    continue
                for command in bindings:
                    planned.setdefault(command, (len(planned) + len(outcomes), name, context))
        # Parameters and every existing warning are authorized before any write.
        edited = tuple(command.for_editor(comms, name.name, project).edited(arguments).with_confirmation(confirmed)
                       for command, (_, name, _) in planned.items())
        for (bound_command, (position, name, context)), command in zip(planned.items(), edited, strict=True):
            try:
                current = comms.registry.snapshot()
                current_catalog = (comms.channels.catalog.read()
                                   if context or name.needs_channel_state else NoCatalog())
                if bound_command not in type(bound_command).bindings(
                        comms, name, context, snapshot=current, catalog=current_catalog,
                        resolved=name.resolve(current, comms.bus.require_channel)):
                    scope = f' in {context}' if context is not None else ''
                    raise CommandSelectionChanged(f'The bound command for {name}{scope} changed; refresh the selection')
                command.schedule(TargetBatchSchedule(position, name, outcomes, together), comms)
            except CLI_FAILURES as error:
                result = TargetFailed(name, type(error).__name__, str(error))
                outcomes.append((position, result))
        for together_declaration, members in together.items():
            results = together_declaration.apply_together(comms, tuple(command for _, _, command in members))
            outcomes.extend((position, TargetCompleted(name, command, result))
                            for (position, name, command), result in zip(members, results, strict=True))
        return TargetBatchResult(tuple(outcome for _, outcome in sorted(outcomes, key=lambda item: item[0])))


class AppliedTogether:
    """A command whose bindings in one target batch are applied as one operation."""

    def schedule(self, batch: TargetBatchSchedule, comms: Comms) -> None:
        batch.together.setdefault(type(self), []).append((batch.position, batch.target, self))

    @classmethod
    @abstractmethod
    def apply_together(cls, ctx: Comms, commands: tuple[Self, ...]) -> tuple[CliResult, ...]: ...


@dataclass(frozen=True)
class TargetEdit(Command[Comms, CliResult]):
    """Accepted editor values use the declared field and current bound command."""
    declaration: type[CliCommand]
    target: TargetSelection
    arguments: dict[str, str | None]
    confirmed: bool = False
    channel: ChannelSelection = NoChannel()
    project: str | None = None

    def apply(self, ctx: Comms) -> CliResult:
        arguments = self.declaration.editor_arguments(self.arguments)
        return self.declaration.execute_target(ctx, self.target, arguments,
                                              confirmed=self.confirmed, channel=self.channel, project=self.project)


class MultipleTargets:
    """Commands accepting a batch own its decoding and execution permission."""

    @classmethod
    def selected_targets(cls, targets: tuple[CommandTarget, ...]) -> TargetSelection:
        """A batch command answers every selection, even of one target, with its batch result."""
        return TargetBatch(tuple(dict.fromkeys(targets)))

    @classmethod
    def require_batch(cls) -> None:
        pass
