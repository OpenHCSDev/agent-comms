"""PreToolUse hook: the commander reaches units only through `order` (send = tracked deadline, say = no work),
and orders tactical work from the unit that owns it instead of running it.

A raw `agent-comms ... send --from <commander>` to a unit bypasses the deadline board and the
wording checks. Core's Todo store says who the units are; anyone else is reached directly.
Installs, maintenance, connector restarts and carries are units' work: run by the commander,
they take its turns away from orders.
"""
import json, re, shlex, sys
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from ..declared_family import DeclaredFamily
from . import order


@dataclass(frozen=True)
class TacticalCommand(DeclaredFamily, affix='Command'):
    """A kind of command a unit owns. Each shape is the words a command runs, in order, matched
    against each simple command's path parts and stems; a quoted multi-word argument such as a
    message body is one word and matches no shape."""
    owner: ClassVar[str] = 'bus-upgrade'
    work: ClassVar[str]
    shapes: ClassVar[tuple[tuple[str, ...], ...]]

    @classmethod
    def ran_by(cls, command: str) -> tuple[type['TacticalCommand'], ...]:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        words, simple = list(lexer), []
        commands = [simple]
        for word in words:
            if word and set(word) <= set(';&|()'):
                commands.append(simple := [])
            elif not any(space in word for space in ' \n\t'):
                simple.extend((*Path(word).parts, Path(word).stem))
        return tuple(kind for kind in cls.members_with(cls)
                     if any(kind.runs(simple) for simple in commands))

    @classmethod
    def runs(cls, words: list[str]) -> bool:
        def ordered(shape):
            remaining = iter(words)
            return all(word in remaining for word in shape)
        return any(ordered(shape) for shape in cls.shapes)

    @classmethod
    def refusal(cls) -> str:
        return (f'{cls.work.capitalize()} is {cls.owner}\'s work, not the commander\'s. '
                f'Send it as an order: agent-comms-commander order send --to {cls.owner}, '
                f'naming the {cls.work} to run.')


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


def commander_session(session: str) -> bool:
    """Claude Code exports its session to every process it starts, the commander's registered
    bus watch among them."""
    process = order.COMMS.registry.require(order.ME).process_identity
    exported = f'CLAUDE_CODE_SESSION_ID={session}'.encode()
    for pid in process.pid_fields().values():
        with suppress(OSError):
            return exported in Path(f'/proc/{pid}/environ').read_bytes().split(b'\0')
    return False


def deny(reason):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
          "permissionDecisionReason": reason}}))


def main(a):
    hook = json.load(sys.stdin)
    command = hook.get('tool_input', {}).get('command', '')
    raw = re.search(r'\bsend\b[^|;&]*--from\s+' + re.escape(order.ME) + r'\b', command)
    targets = set(re.findall(r'--to\s+(\S+)', command))
    if raw and 'agent-comms-commander' not in command and targets & order.units():
        return deny("Raw send to a unit. Use agent-comms-commander order send (orders, with deadline) or order say (no work).")
    if (kinds := TacticalCommand.ran_by(command)) and commander_session(hook.get('session_id', '')):
        deny(' '.join(kind.refusal() for kind in kinds))
