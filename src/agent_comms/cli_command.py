"""Declaration-owned CLI operation and its required dependencies."""

from __future__ import annotations

from abc import abstractmethod
from agent_comms.channel_targets import ChannelSelection
from agent_comms.channel_targets import CommandTarget
from agent_comms.channel_targets import NoChannel
from agent_comms.channel_targets import OneChannel
from agent_comms.channel_targets import ResolvedChannel
from agent_comms.cli_result import CliResult
from agent_comms.command import Command
from agent_comms.declared_verb import DeclaredCommand
from agent_comms.field_codec import FieldCodec
from agent_comms.field_codec import FieldPlan
from agent_comms.field_codec import TargetBound
from agent_comms.launch_root import LaunchRoot
from dataclasses import dataclass
from inspect import iscoroutinefunction
from inspect import signature
from metaclass_registry import LazyDiscoveryDict
from types import get_original_bases
from typing import Self
from typing import TYPE_CHECKING
from typing import TextIO
from typing import cast
from typing import get_args
from typing import get_origin
import os

if TYPE_CHECKING:
    from agent_comms.active_route import ActiveRoute, CommsRoute
    from agent_comms.catalog_document import CatalogDocument
    from agent_comms.cli_commands import OneTarget
    from agent_comms.cli_commands import TargetAction
    from agent_comms.cli_commands import TargetBatch
    from agent_comms.cli_commands import TargetBatchSchedule
    from agent_comms.cli_commands import TargetCompleted
    from agent_comms.cli_commands import TargetSelection
    from agent_comms.comms import Comms
    from agent_comms.config_options import ConfigOptions
    from agent_comms.registry_document import RegistrySnapshot
    from agent_comms.target_fields import TargetField
    from agent_comms.thread_status import ThreadStatus
    from agent_comms.threads import Thread


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
    @classmethod
    def cli_members(cls, selected: str | None) -> tuple[type[Self], ...]:
        """The parser loads selected declarations through the family's discovery owner."""
        if selected is None:
            return cls.members_with(cls)
        registry = cast(LazyDiscoveryDict, cls.__registry__)
        registry.discover_matching(
            lambda module: module.rsplit(".", 1)[1].replace("_", "-") == selected.replace("_", "-"))
        return tuple(cast(type[Self], member) for key, member in dict.items(registry)
                     if key == selected)

    def schedule(self, batch: TargetBatchSchedule, comms: Comms) -> None:
        from agent_comms.cli_commands import TargetCompleted

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
        from agent_comms.cli_commands import OneTarget, TargetBatch

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
            if entry.key == "channel" and entry.setting(TargetBound, False)
        )

    @classmethod
    def selection_channels(cls, channel: ChannelSelection,
                           target: str) -> tuple[str | None, ...]:
        return channel.channels_for(target)

    @classmethod
    def thread_bindings(cls, comms: Comms, thread: Thread, status: ThreadStatus, channel: str | None = None, *, catalog: CatalogDocument | None = None) -> tuple[Self, ...]:
        return ()

    @classmethod
    def project_bindings(cls, comms: Comms, project: str) -> tuple[Self, ...]:
        return ()

    @classmethod
    def channel_bindings(cls, comms: Comms, resolved: ResolvedChannel, *, snapshot: RegistrySnapshot, catalog: CatalogDocument | None) -> tuple[Self, ...]:
        return ()

    @classmethod
    def bindings(cls, comms: Comms, target: CommandTarget, channel: str | None = None,
                 *, snapshot: RegistrySnapshot, catalog: CatalogDocument | None,
                 resolved) -> tuple[Self, ...]:
        return target.bindings(
            cls, comms, channel, resolved, snapshot=snapshot, catalog=catalog)

    @classmethod
    def target_catalog(cls, comms: Comms, target: TargetSelection,
                       channel: ChannelSelection = NoChannel(),
                       *, project: str) -> tuple[TargetAction, ...]:
        from agent_comms.cli_commands import TargetAction

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
                   if channel.needs_catalog or any(item.needs_channel_state for item in selected) else None)
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
        from agent_comms.target_fields import TargetField

        return tuple(
            TargetField.of(entry, entry.current(self))
            for entry in FieldCodec.argument_fields(type(self))
            if not entry.setting(TargetBound, False)
        )

    async def prepare_fields(self) -> tuple[TargetField, ...]:
        from agent_comms.config_options import ConfigOptions

        catalogs = ConfigOptions.configured()
        return tuple([await item.prepare(catalogs, self) for item in self.editable_fields])

    def describe(self, comms: Comms, target: CommandTarget, project: str) -> TargetAction:
        from agent_comms.cli_commands import TargetAction

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
        from agent_comms.active_route import ActiveRoute

        return ActiveRoute.published_launch()

    def execute(self, route: CommsRoute, output: TextIO) -> int:
        """Open the bus this command reads, then run it; a command that must act before
        the bus opens overrides this."""
        from agent_comms.comms import Comms

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
                "kind": FieldCodec.encode(type(self)),
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
                   if channel.needs_catalog or any(item.needs_channel_state for item in target.targets) else None)
        return target.run(cls, comms, arguments, confirmed=confirmed, channel=channel,
                             project=os.getcwd() if project is None else project,
                             snapshot=snapshot, catalog=catalog)
