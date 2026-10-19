"""PreToolUse hook: the commander reaches units only through `order` (send = tracked deadline,
say = no work), and orders tactical work from the unit that owns it instead of running it.

A raw `agent-comms ... send --from <commander>` to a unit bypasses the deadline board and the
wording checks. Core's Todo store says who the units are; anyone else is reached directly.
Installs, maintenance, connector restarts and carries are the installer's work: run by the
commander, they take its turns away from orders.

The command line is parsed once into simple commands of words; a quoted multi-word argument
such as a message body is one word, so a send or a command named inside it runs nothing.
"""
from __future__ import annotations

import inspect
import json
import shlex
from abc import abstractmethod
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import ClassVar

from agent_comms_hooks import ClaudeBashHook

from ..declared_family import DeclaredFamily
from ..errors import RelationViolationError
from ..todos import InstallDeadline
from .config import Commander, CommanderConfig

Words = tuple[str, ...]


@dataclass(frozen=True)
class SimpleCommand:
    words: Words


class ParsedCommand(SimpleCommand):
    """The shell lexer identified this simple command's argument boundaries."""


class UnparsedCommand(SimpleCommand):
    """Malformed shell input still exposes its whitespace-separated words to rules."""


def simple_commands(command: str) -> tuple[SimpleCommand, ...]:
    """The one parse of a command line: its simple commands, split at shell operators."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    commands: list[list[str]] = [[]]
    try:
        for word in lexer:
            if word and set(word) <= set(';&|()'):
                commands.append([])
            else:
                commands[-1].append(word)
    except ValueError:
        return (UnparsedCommand(tuple(command.split())),)
    return tuple(ParsedCommand(tuple(words)) for words in commands if words)


@dataclass(frozen=True)
class CommanderSession:
    """The hook's Claude session, classified against the session Claude Code exported to the
    commander's registered process. A session that cannot be classified is refused."""
    session: str
    commander: Commander

    def is_commander(self) -> bool:
        name = self.commander.name
        process = self.commander.comms.registry.require(name).process_identity.require(
            f'{name} has no registered process to compare this session with')
        environment = Path(f'/proc/{process.pid}/environ').read_bytes().split(b'\0')
        return f'CLAUDE_CODE_SESSION_ID={self.session}'.encode() in environment

    def refusals(self, kind: type[TacticalCommand]) -> tuple[str, ...]:
        # A gone process has no environ, and another user's cannot be read.
        try:
            commander = self.is_commander()
        except (FileNotFoundError, PermissionError, RelationViolationError) as error:
            return (f'{kind.work.capitalize()} refused: the guard cannot tell whether this '
                    f'session is {self.commander.name} ({error}).',)
        return kind.install_refusals(self.commander) if commander else ()


@dataclass(frozen=True)
class GuardedCommand(DeclaredFamily, affix='Command'):
    """A kind of simple command the guard refuses in some sessions."""

    @classmethod
    @abstractmethod
    def refused(cls, words: Words, session: CommanderSession) -> tuple[str, ...]: ...

    @classmethod
    def refusals(cls, command: str, session: CommanderSession) -> tuple[str, ...]:
        return tuple(reason for simple in simple_commands(command)
                     for kind in cls.members_with(cls) if not inspect.isabstract(kind)
                     for reason in kind.refused(simple.words, session))


@dataclass(frozen=True)
class RawSendCommand(GuardedCommand):
    """`send --from <commander> --to <unit>` outside the commander's order tool, in any session."""

    @classmethod
    def refused(cls, words: Words, session: CommanderSession) -> tuple[str, ...]:
        pairs = set(pairwise(words))
        if 'send' not in words or 'agent-comms-commander' in {Path(word).name for word in words}:
            return ()
        addressed = pairs & {('--to', unit) for unit in session.commander.units()}
        if ('--from', session.commander.name) not in pairs or not addressed:
            return ()
        return ('Raw send to a unit. Use agent-comms-commander order send '
                '(orders, with deadline) or order say (no work).',)


@dataclass(frozen=True)
class TacticalCommand(GuardedCommand):
    """Work the bus's planned installer owns. Each shape is the words a command runs,
    in order, matched against its words' path parts and stems."""
    work: ClassVar[str] = 'tactical work'
    shapes: ClassVar[tuple[Words, ...]] = ()

    @classmethod
    def refused(cls, words: Words, session: CommanderSession) -> tuple[str, ...]:
        return session.refusals(cls) if cls.runs(words) else ()

    @classmethod
    def runs(cls, words: Words) -> bool:
        parts = [part for word in words if not any(space in word for space in ' \n\t')
                 for part in (*Path(word).parts, Path(word).stem)]

        def ordered(shape: Words) -> bool:
            remaining = iter(parts)
            return all(word in remaining for word in shape)
        return any(ordered(shape) for shape in cls.shapes)

    @classmethod
    def install_refusals(cls, commander: Commander) -> tuple[str, ...]:
        return InstallDeadline.read(commander.comms.root).refusals(cls.work, commander.name)


@dataclass(frozen=True)
class InstallCommand(TacticalCommand):
    work = 'install'
    shapes = (('relink',), ('publish_runtime',), ('agent-comms', 'install'),
              ('pip', 'install'), ('uv', 'install'))


@dataclass(frozen=True)
class MaintenanceCommand(TacticalCommand):
    work = 'maintenance'
    shapes = (('upgrade_bus',),)


@dataclass(frozen=True)
class ConnectorRestartCommand(TacticalCommand):
    work = 'connector restart'
    shapes = (('systemctl', 'restart', 'agent-comms-connector'),
              ('systemctl', 'start', 'agent-comms-connector'),
              ('systemctl', 'stop', 'agent-comms-connector'),
              ('agent-comms-connector', 'serve'))


@dataclass(frozen=True)
class CarryCommand(TacticalCommand):
    work = 'carry'
    shapes = (('cutover',),)


def main(a: list[str]) -> None:
    hook = ClaudeBashHook.read()
    session = CommanderSession(hook.session_id, Commander.of(CommanderConfig.installed()))
    reasons = GuardedCommand.refusals(hook.tool_input.command, session)
    if reasons:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": hook.hook_event_name, "permissionDecision": "deny",
            "permissionDecisionReason": ' '.join(reasons)}}))
