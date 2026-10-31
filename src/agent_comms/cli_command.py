from __future__ import annotations

from abc import ABC, abstractmethod
from agent_comms.active_route import ActiveRoute
from agent_comms.catalog_document import CatalogAccess, NoCatalog
from agent_comms.channel_targets import ChannelSelection, CommandTarget, NoChannel, SurroundingChannel
from agent_comms.cli_failures import CLI_FAILURES
from agent_comms.cli_result import CliResult, ResultShower
from agent_comms.command import Command
from agent_comms.comms import Comms
from agent_comms.repo import Checkout
from agent_comms.config_options import ConfigOptions
from agent_comms.nominal.field_codec import FieldCodec, RecordMember, WireValue
from agent_comms.installer.owner_starts import OwnerStart, RefusedOwner, StartedOwner
from agent_comms.lifecycle_capability import LifecycleCapability, Successful
from agent_comms.registry_document import RegistrySnapshot
from agent_comms.target_fields import TargetField
from agent_comms.thread_model import CatalogSelection
from dataclasses import dataclass, field, replace
from agent_comms_hooks import ToolRefusedError
from pydantic import JsonValue
from collections.abc import Mapping
from typing import Annotated, Generic, Self, TypeVar

from agent_comms.channel_targets import OneChannel, ResolvedChannel, ResolvedThread
from agent_comms.declared_verb import DeclaredCommand
from agent_comms.nominal.declared_family import ModuleMembers
from agent_comms.nominal.field_codec import FieldPlan, TargetBound
from agent_comms.launch_root import LaunchRoot
from functools import singledispatchmethod
from inspect import iscoroutinefunction, signature
from types import MappingProxyType, get_original_bases
from typing import TYPE_CHECKING, TextIO, cast, get_args, get_origin

if TYPE_CHECKING:
    from agent_comms.active_route import CommsRoute
    from agent_comms.thread_status import ThreadStatus
    from agent_comms.threads import Thread


class ChannelBound(TargetBound):
    """A target-bound field supplies the surrounding channel scope."""


class CliExecution[Answer: CliResult = CliResult](Command[Comms, Answer]):
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


Answer = TypeVar("Answer", bound=CliResult, covariant=True, default=CliResult)


class CommandBinding(ABC):
    """A declared operation can collect fields before a complete command exists."""

    @property
    @abstractmethod
    def declaration(self) -> type[CliCommand]: ...

    @property
    def editable_fields(self) -> tuple[TargetField, ...]:
        return tuple(self.field_value(entry)
                     for entry in FieldCodec.argument_fields(self.declaration)
                     if not entry.setting(TargetBound, False))

    @abstractmethod
    def field_value(self, entry: FieldPlan) -> TargetField: ...

    @abstractmethod
    def retained_payload(self) -> dict[str, object]:
        """Already decoded constructor values, never serialized for editing."""

    def for_editor(self, comms: Comms, target: str, project: Checkout) -> Self:
        return self

    def for_fields(self, arguments: Mapping[str, object]) -> CommandBinding:
        return self.edited(self.declaration.editor_arguments(arguments)) if arguments else self

    async def prepare_field(self, item: TargetField, catalogs: ConfigOptions) -> TargetField:
        return await item.prepare(catalogs)

    async def prepare_fields(
        self, arguments: Mapping[str, object] = MappingProxyType({}),
    ) -> tuple[TargetField, ...]:
        binding = self.for_fields(arguments)
        catalogs = ConfigOptions.configured()
        return tuple([await binding.prepare_field(item, catalogs)
                      for item in binding.editable_fields])

    def authorize_edits(self, arguments: Mapping[str, object]) -> frozenset[str]:
        bound = frozenset(entry.key for entry in FieldCodec.argument_fields(self.declaration)
                          if entry.setting(TargetBound, False))
        if bound & arguments.keys():
            raise ValueError("Target-bound parameters cannot be overridden")
        if arguments.keys() - {item.name for item in self.editable_fields}:
            raise ValueError("Parameters not offered by this target command cannot be overridden")
        return bound

    @staticmethod
    def decoded_edits(declaration: type, arguments: Mapping[str, object]) -> dict[str, object]:
        return {name: value
                for entry in FieldCodec.argument_fields(declaration) if entry.key in arguments
                for name, value in entry.decoded(arguments).items()}

    def edited(self, arguments: Mapping[str, object]) -> CliCommand:
        self.authorize_edits(arguments)
        values = {**self.retained_payload(), **self.decoded_edits(self.declaration, arguments)}
        for parameter in self.editable_fields:
            parameter.plan.require_constructor_input(values)
        return FieldCodec.construct(self.declaration, values)

    @abstractmethod
    def confirmation(self) -> str: ...

    @classmethod
    @abstractmethod
    def selection_confirmation(cls, commands: tuple[Self, ...]) -> str: ...

    @abstractmethod
    def with_confirmation(self, confirmed: bool) -> CommandBinding: ...


class CatalogBinding(CommandBinding):
    """Only a binding declaring catalog inputs has a catalog question to answer."""

    @property
    @abstractmethod
    def catalog_context(self) -> CatalogSelection: ...

    async def prepare_field(self, item: TargetField, catalogs: ConfigOptions) -> TargetField:
        return await item.prepare_catalog(catalogs, self.catalog_context)


@dataclass(frozen=True)
class CommandContext(CatalogSelection):
    """A declaration of supplied fields, not an alternate command editor; the one declared
    command inheriting it owns the fields it lacks."""

    @classmethod
    def command(cls) -> type[CliCommand]:
        (command,) = CliCommand.members_with(cls)
        return command


@dataclass(frozen=True)
class PendingCommandBinding(CatalogBinding):
    """Only supplied context, borrowing the concrete command's declared fields."""

    context: CommandContext

    @property
    def declaration(self) -> type[CliCommand]:
        return type(self.context).command()

    @property
    def catalog_context(self) -> CatalogSelection:
        return self.context

    def retained_payload(self) -> dict[str, object]:
        return {entry.name: entry.current(self.context)
                for entry in FieldCodec.field_plans(type(self.context))}

    def field_value(self, entry: FieldPlan) -> TargetField:
        for supplied in FieldCodec.field_plans(type(self.context)):
            if supplied.declared is entry.declared:
                return TargetField.of(entry, supplied.current(self.context))
        return TargetField.omitted(entry)

    def for_fields(self, arguments: Mapping[str, object]) -> PendingCommandBinding:
        self.authorize_edits(arguments)
        context_fields = {entry.key for entry in FieldCodec.argument_fields(type(self.context))}
        wire = FieldCodec.editor_arguments(type(self.context), {
            key: value for key, value in arguments.items() if key in context_fields
        })
        changes = self.decoded_edits(type(self.context), wire)
        return replace(self, context=replace(self.context, **changes)) if changes else self

    def confirmation(self) -> str:
        return ''

    @classmethod
    def selection_confirmation(cls, commands: tuple[Self, ...]) -> str:
        return ''

    def with_confirmation(self, confirmed: bool) -> CliCommand:
        return self.edited({}).with_confirmation(confirmed)


@dataclass(frozen=True, kw_only=True)
class CliCommand(
    ModuleMembers,
    DeclaredCommand,
    CliExecution[Answer],
    CommandBinding,
    Generic[Answer],
    affix="CliCommand",
    discovery_package="agent_comms.cli_verbs",
):
    @property
    def declaration(self) -> type[Self]:
        return type(self)

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
        commands = cls.__mro__[:cls.__mro__.index(CliCommand) + 1]
        for declaration in commands:
            for base in get_original_bases(declaration):
                if get_origin(base) in commands:
                    (answer,) = get_args(base)
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
    def thread_bindings(cls, comms: Comms, thread: Thread, status: ThreadStatus, channel: SurroundingChannel, *, catalog: CatalogAccess) -> tuple[CommandBinding, ...]:
        return ()

    @classmethod
    def project_bindings(cls, comms: Comms, project: Checkout) -> tuple[CommandBinding, ...]:
        return ()

    @classmethod
    def channel_bindings(cls, comms: Comms, resolved: ResolvedChannel, *, project: Checkout, snapshot: RegistrySnapshot, catalog: CatalogAccess) -> tuple[CommandBinding, ...]:
        return ()

    @singledispatchmethod
    @classmethod
    def bindings(cls, resolved: object, comms: Comms, channel: SurroundingChannel,
                 *, project: Checkout, snapshot: RegistrySnapshot,
                 catalog: CatalogAccess) -> tuple[CommandBinding, ...]:
        """The resolved target's type selects which declared bindings answer it."""
        raise TypeError(f"{type(resolved).__qualname__} is not a resolved command target")

    @bindings.register
    @classmethod
    def _thread_bindings(cls, resolved: ResolvedThread, comms: Comms, channel: SurroundingChannel,
                         *, project: Checkout, snapshot: RegistrySnapshot,
                         catalog: CatalogAccess) -> tuple[CommandBinding, ...]:
        return cls.thread_bindings(
            comms, resolved.thread, resolved.status, channel, catalog=catalog)

    @bindings.register
    @classmethod
    def _channel_bindings(cls, resolved: ResolvedChannel, comms: Comms, channel: SurroundingChannel,
                          *, project: Checkout, snapshot: RegistrySnapshot,
                          catalog: CatalogAccess) -> tuple[CommandBinding, ...]:
        return cls.channel_bindings(
            comms, resolved, project=project, snapshot=snapshot, catalog=catalog)

    @bindings.register
    @classmethod
    def _project_bindings(cls, resolved: Checkout, comms: Comms, channel: SurroundingChannel,
                          *, project: Checkout, snapshot: RegistrySnapshot,
                          catalog: CatalogAccess) -> tuple[CommandBinding, ...]:
        return cls.project_bindings(comms, resolved)

    @classmethod
    def target_catalog(cls, comms: Comms, target: TargetSelection,
                       channel: ChannelSelection = NoChannel(),
                       *, project: Checkout) -> tuple[TargetAction, ...]:

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
        grouped: dict[type[CliCommand], dict[CommandBinding, None]] = {}
        for member in target.command_members(cls):
            selected_channels = channel if member.channel_fields() else NoChannel()
            for name, resolved in resolved_targets:
                for context in name.contexts(member, selected_channels):
                    available = member.bindings(
                        resolved, comms, context,
                        project=project, snapshot=snapshot, catalog=catalog)
                    if available:
                        declaration = target.listed_declaration(member)
                        grouped.setdefault(declaration, {}).update(dict.fromkeys(
                            command.for_editor(comms, name.name, project) for command in available))
        names = tuple(name for item in selected for name in item.destination_names)
        return tuple(TargetAction(tuple(commands), names, declaration,
                                  channel if declaration.channel_fields() else NoChannel())
                     for declaration, commands in grouped.items())

    def field_value(self, entry: FieldPlan) -> TargetField:
        return TargetField.of(entry, entry.current(self))

    def retained_payload(self) -> dict[str, object]:
        return {entry.name: entry.current(self)
                for entry in FieldCodec.argument_fields(type(self))
                if entry.setting(TargetBound, False)}

    def describe(self, comms: Comms, target: CommandTarget, project: Checkout) -> TargetAction:

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
    def editor_arguments(cls, values: Mapping[str, object]) -> dict[str, object]:
        return FieldCodec.editor_arguments(cls, values)

    def confirmation(self) -> str:
        return ''

    @classmethod
    def selection_confirmation[C: CliCommand](cls: type[C], commands: tuple[C, ...]) -> str:
        return '\n'.join(dict.fromkeys(warning for command in commands
                                      if (warning := command.confirmation())))

    def with_confirmation(self, confirmed: bool) -> Self:
        if self.confirmation() and not confirmed:
            raise ValueError(self.confirmation())
        return self

    @classmethod
    def execute_target(cls, comms: Comms, target: TargetSelection, arguments: Mapping[str, object],
                       *, confirmed: bool = False,
                       channel: ChannelSelection = NoChannel(),
                       project: Checkout) -> CliResult:
        if not cls.channel_fields():
            channel.require_unbound()
            channel = NoChannel()
        snapshot = comms.registry.snapshot()
        catalog = (comms.channels.catalog.read()
                   if channel.needs_catalog or any(item.needs_channel_state for item in target.targets) else NoCatalog())
        return target.run(cls, comms, arguments, confirmed=confirmed, channel=channel,
                             project=project,
                             snapshot=snapshot, catalog=catalog)


@dataclass(frozen=True)
class TargetAction:
    """A selection keeps the bound commands, not a separate permission list."""
    bound: tuple[CommandBinding, ...]
    targets: tuple[str, ...]
    declaration: type[CliCommand]
    channels: ChannelSelection = NoChannel()

    @property
    def editable_fields(self) -> tuple[TargetField, ...]:
        return self.bound[0].editable_fields

    async def prepare_fields(
        self, arguments: Mapping[str, object] = MappingProxyType({}),
    ) -> tuple[TargetField, ...]:
        return await self.bound[0].prepare_fields(arguments)

    @property
    def label(self) -> str:
        return self.declaration.help

    @property
    def confirmation(self) -> str:
        groups: dict[type[CommandBinding], list[CommandBinding]] = {}
        for command in self.bound:
            groups.setdefault(type(command), []).append(command)
        return '\n'.join(filter(None, (declaration.selection_confirmation(tuple(commands))
                                       for declaration, commands in groups.items())))

    def edited(self, arguments: Mapping[str, object]) -> TargetAction:
        return replace(self, bound=tuple(command.edited(command.declaration.editor_arguments(arguments))
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
    def from_wire(cls, data: object) -> Self:
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
    def run(self, declaration: type[CliCommand], comms: Comms, arguments: Mapping[str, object],
            *, confirmed: bool, channel: ChannelSelection, project: Checkout,
            snapshot: RegistrySnapshot, catalog: CatalogAccess) -> CliResult:
        """Bind, authorize and apply the declaration on this selection."""


class OneTarget(TargetSelection):
    """A single target: resolve errors raise and the bound command's own result returns."""

    def __post_init__(self) -> None:
        super().__post_init__()
        if len(self.targets) != 1:
            raise ValueError('This action requires a single target')

    def run(self, declaration: type[CliCommand], comms: Comms, arguments: Mapping[str, object], *, confirmed: bool, channel: ChannelSelection, project: Checkout, snapshot: RegistrySnapshot, catalog: CatalogAccess) -> CliResult:
        name, = self.targets
        resolved = name.resolve(snapshot, comms.bus.require_channel)
        bindings = tuple(bound for context in name.contexts(declaration, channel)
                         for bound in declaration.bindings(
                             resolved, comms, context,
                             project=project, snapshot=snapshot, catalog=catalog))
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

    def run(self, declaration: type[CliCommand], comms: Comms, arguments: Mapping[str, object], *, confirmed: bool, channel: ChannelSelection, project: Checkout, snapshot: RegistrySnapshot, catalog: CatalogAccess) -> CliResult:
        declaration.require_batch()
        declarations = (tuple(member for member in CliCommand.members_with(CliCommand)
                              if member.catalog_declaration() is declaration)
                        if declaration.catalog_declaration() is declaration else (declaration,))
        outcomes: list[tuple[int, TargetCompleted | TargetFailed]] = []
        planned: dict[CommandBinding, tuple[int, CommandTarget, SurroundingChannel]] = {}
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
                                     for command in member.bindings(
                                         resolved, comms, context,
                                         project=project, snapshot=snapshot, catalog=catalog))
                    if not bindings:
                        raise CommandSelectionChanged(
                            f'This action is no longer available for {name}{context.scope()}')
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
                                   if context.needs_catalog or name.needs_channel_state else NoCatalog())
                if bound_command not in bound_command.declaration.bindings(
                        name.resolve(current, comms.bus.require_channel), comms, context,
                        project=project, snapshot=current, catalog=current_catalog):
                    raise CommandSelectionChanged(
                        f'The bound command for {name}{context.scope()} changed; refresh the selection')
                command.schedule(TargetBatchSchedule(position, name, outcomes, together), comms)
            except CLI_FAILURES as error:
                result = TargetFailed(name, type(error).__name__, str(error))
                outcomes.append((position, result))
        for together_declaration, members in together.items():
            results = together_declaration.apply_together(comms, tuple(command for _, _, command in members))
            outcomes.extend((position, TargetCompleted(name, command, result))
                            for (position, name, command), result in zip(members, results, strict=True))
        return TargetBatchResult(tuple(outcome for _, outcome in sorted(outcomes, key=lambda item: item[0])))


class AppliedTogether(CliCommand[Answer]):
    """A command whose bindings in one target batch are applied as one operation."""

    def apply(self, ctx: Comms) -> Answer:
        return self.apply_together(ctx, (self,))[0]

    def schedule(self, batch: TargetBatchSchedule, comms: Comms) -> None:
        batch.together.setdefault(type(self), []).append((batch.position, batch.target, self))

    @classmethod
    @abstractmethod
    def apply_together(cls, ctx: Comms, commands: tuple[Self, ...]) -> tuple[Answer, ...]: ...


@dataclass(frozen=True)
class TargetEdit(Command[Comms, CliResult]):
    """Accepted editor values use the declared field and current bound command."""
    declaration: type[CliCommand]
    target: TargetSelection
    arguments: Mapping[str, object]
    confirmed: bool = False
    channel: ChannelSelection = NoChannel()
    project: Checkout = field(kw_only=True)

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
