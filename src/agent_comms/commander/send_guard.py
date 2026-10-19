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
import sys
from abc import abstractmethod
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import ClassVar

from ..declared_family import DeclaredFamily
from ..errors import RelationViolationError
from ..todos import TodoError
from . import order

Words = tuple[str, ...]


def simple_commands(command: str) -> tuple[Words, ...]:
    """The one parse of a command line: its simple commands, split at shell operators."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    commands: list[list[str]] = [[]]
    for word in lexer:
        if word and set(word) <= set(';&|()'):
            commands.append([])
        else:
            commands[-1].append(word)
    return tuple(tuple(words) for words in commands if words)


@dataclass(frozen=True)
class CommanderSession:
    """The hook's Claude session, classified against the session Claude Code exported to the
    commander's registered process. A session that cannot be classified is refused."""
    session: str

    def commander(self) -> bool:
        process = order.COMMS.registry.require(order.ME).process_identity.require(
            f'{order.ME} has no registered process to compare this session with')
        environment = Path(f'/proc/{process.pid}/environ').read_bytes().split(b'\0')
        return f'CLAUDE_CODE_SESSION_ID={self.session}'.encode() in environment

    def refusals(self, kind: type[TacticalCommand]) -> tuple[str, ...]:
        try:
            return (kind.refusal(),) if self.commander() else ()
        except (OSError, RelationViolationError) as error:
            return (f'{kind.work.capitalize()} refused: the guard cannot tell whether this '
                    f'session is {order.ME} ({error}).',)


@dataclass(frozen=True)
class GuardedCommand(DeclaredFamily, affix='Command'):
    """A kind of simple command the guard refuses in some sessions."""

    @classmethod
    @abstractmethod
    def refused(cls, words: Words, session: CommanderSession) -> tuple[str, ...]: ...

    @classmethod
    def refusals(cls, command: str, session: CommanderSession) -> tuple[str, ...]:
        return tuple(reason for words in simple_commands(command)
                     for kind in cls.members_with(cls) if not inspect.isabstract(kind)
                     for reason in kind.refused(words, session))


@dataclass(frozen=True)
class RawSendCommand(GuardedCommand):
    """`send --from <commander> --to <unit>` outside the commander's order tool, in any session."""

    @classmethod
    def refused(cls, words: Words, session: CommanderSession) -> tuple[str, ...]:
        pairs = set(pairwise(words))
        targets = {value for flag, value in pairs if flag == '--to'}
        if not ('send' in words and ('--from', order.ME) in pairs and targets & order.units()
                and 'agent-comms-commander' not in {Path(word).name for word in words}):
            return ()
        return ('Raw send to a unit. Use agent-comms-commander order send '
                '(orders, with deadline) or order say (no work).',)


@dataclass(frozen=True)
class TacticalCommand(GuardedCommand):
    """Work the unit holding the installer problem owns. Each shape is the words a command runs,
    in order, matched against its words' path parts and stems."""
    problem: ClassVar[str] = 'P168'
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
    def refusal(cls) -> str:
        try:
            owner = order.COMMS.goals.todos.get(cls.problem).held().owner
        except TodoError as error:
            return (f'{cls.work.capitalize()} belongs to the holder of {cls.problem}, and there is '
                    f'none ({error}). Order {cls.problem} to a unit instead of running it.')
        return (f"{cls.work.capitalize()} is {owner}'s work, as holder of {cls.problem}, not "
                f"{order.ME}'s. Send it as an order: agent-comms-commander order send --to "
                f'{owner}, naming the {cls.work} to run.')


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
    hook = json.load(sys.stdin)
    reasons = GuardedCommand.refusals(hook.get('tool_input', {}).get('command', ''),
                                      CommanderSession(hook.get('session_id', '')))
    if reasons:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "deny",
            "permissionDecisionReason": ' '.join(reasons)}}))
