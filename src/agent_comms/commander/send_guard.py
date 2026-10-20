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
from abc import abstractmethod
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from agent_comms_hooks import ClaudeBashHook, ClaudeFileEditHook

from ..declared_family import DeclaredFamily
from ..errors import RelationViolationError
from ..storage_rules import GitProgram, ShellInterpreter, ShellInvocation
from ..todos import InstallDeadline

if TYPE_CHECKING:
    from .order import Commander

Words = tuple[str, ...]


@dataclass(frozen=True)
class CommanderSession:
    """The hook's Claude session, classified against the session Claude Code exported to the
    commander's registered process. A session that cannot be classified is refused."""

    session: str
    commander: Commander

    def is_commander(self) -> bool:
        name = self.commander.commander
        process = self.commander.comms.registry.require(name).process_identity.require(
            f"{name} has no registered process to compare this session with"
        )
        environment = Path(f"/proc/{process.pid}/environ").read_bytes().split(b"\0")
        return f"CLAUDE_CODE_SESSION_ID={self.session}".encode() in environment

    def refusals(self, kind: type[TacticalCommand]) -> tuple[str, ...]:
        # A gone process has no environ, and another user's cannot be read.
        try:
            commander = self.is_commander()
        except (FileNotFoundError, PermissionError, RelationViolationError) as error:
            return (
                f"{kind.work.capitalize()} refused: the guard cannot tell whether this "
                f"session is {self.commander.commander} ({error}).",
            )
        return kind.install_refusals(self.commander) if commander else ()


@dataclass(frozen=True)
class GuardedCommand(DeclaredFamily, affix="Command"):
    """A kind of simple command the guard refuses in some sessions."""

    @classmethod
    @abstractmethod
    def refused(cls, words: Words, session: CommanderSession, cwd: Path) -> tuple[str, ...]: ...

    @classmethod
    def refusals(cls, command: str, session: CommanderSession, cwd: Path) -> tuple[str, ...]:
        return tuple(
            reason
            for invocation in ShellInvocation.parse(command, cwd)
            for kind in cls.members_with(cls)
            if not inspect.isabstract(kind)
            for reason in kind.refused(invocation.words, session, invocation.cwd)
        )


@dataclass(frozen=True)
class RawSendCommand(GuardedCommand):
    """`send --from <commander> --to <unit>` outside the commander's order tool, in any session."""

    @classmethod
    def refused(cls, words: Words, session: CommanderSession, cwd: Path) -> tuple[str, ...]:
        pairs = set(pairwise(words))
        if "send" not in words or "agent-comms-commander" in {Path(word).name for word in words}:
            return ()
        addressed = pairs & {("--to", unit) for unit in session.commander.units()}
        if ("--from", session.commander.commander) not in pairs or not addressed:
            return ()
        return (
            "Raw send to a unit. Use agent-comms-commander order send "
            "(orders, with deadline) or order say (no work).",
        )


@dataclass(frozen=True)
class TacticalCommand(GuardedCommand):
    """Work the bus's planned installer owns. Each shape is the words a command runs,
    in order, matched against its words' path parts and stems."""

    work: ClassVar[str] = "tactical work"
    shapes: ClassVar[tuple[Words, ...]] = ()

    @classmethod
    def refused(cls, words: Words, session: CommanderSession, cwd: Path) -> tuple[str, ...]:
        return session.refusals(cls) if cls.runs(words) else ()

    @classmethod
    def runs(cls, words: Words) -> bool:
        parts = [
            part
            for word in words
            if not any(space in word for space in " \n\t")
            for part in (*Path(word).parts, Path(word).stem)
        ]

        def ordered(shape: Words) -> bool:
            remaining = iter(parts)
            return all(word in remaining for word in shape)

        return any(ordered(shape) for shape in cls.shapes)

    @classmethod
    def install_refusals(cls, ctx: Commander) -> tuple[str, ...]:
        return InstallDeadline.read(ctx.comms.root).refusals(cls.work, ctx.commander)


@dataclass(frozen=True)
class InstallCommand(TacticalCommand):
    work = "install"
    shapes = (
        ("relink",),
        ("publish_runtime",),
        ("agent-comms", "install"),
        ("pip", "install"),
        ("uv", "install"),
    )


@dataclass(frozen=True)
class MaintenanceCommand(TacticalCommand):
    work = "maintenance"
    shapes = (("upgrade_bus",),)


@dataclass(frozen=True)
class ConnectorRestartCommand(TacticalCommand):
    work = "connector restart"
    shapes = (
        ("systemctl", "restart", "agent-comms-connector"),
        ("systemctl", "start", "agent-comms-connector"),
        ("systemctl", "stop", "agent-comms-connector"),
        ("agent-comms-connector", "serve"),
    )


@dataclass(frozen=True)
class CarryCommand(TacticalCommand):
    work = "carry"
    shapes = (("cutover",),)


@dataclass(frozen=True)
class SourceChangeCommand(TacticalCommand):
    """Direct fixes are refused independently of planned installation authority."""

    work = "source change"
    reason: ClassVar[str] = (
        "the commander does not write fixes: order the edit to a unit or start a fork"
    )
    landing_tool: ClassVar[Path] = Path(__file__).resolve().parents[3] / "tools/batch_land.sh"

    @classmethod
    def protected(cls, path: Path, ctx: Commander) -> bool:
        path = path.resolve()
        if any(path.is_relative_to(root.resolve()) for root in (ctx.scratch, ctx.state)):
            return False
        return any(
            path.is_relative_to(root.resolve())
            for root in (ctx.worktrees, *(repo.checkout for repo in ctx.repos))
        )

    @classmethod
    def landing(cls, words: Words, cwd: Path) -> bool:
        script = (
            words[1] if Path(words[0]).name in ShellInterpreter and len(words) > 1 else words[0]
        )
        path = Path(script).expanduser()
        return (path if path.is_absolute() else cwd / path).resolve() == cls.landing_tool

    @classmethod
    def refused(cls, words: Words, session: CommanderSession, cwd: Path) -> tuple[str, ...]:
        if cls.landing(words, cwd) or Path(words[0]).name not in GitProgram.names:
            return ()
        invocation = GitProgram.invocation(list(words[1:]), cwd)
        if invocation.writes_source and any(
            cls.protected(path, session.commander) for path in invocation.directories
        ):
            return session.refusals(cls)
        return ()

    @classmethod
    def install_refusals(cls, ctx: Commander) -> tuple[str, ...]:
        return (cls.reason,)


class GuardTool(DeclaredFamily, affix="GuardTool"):
    @classmethod
    @abstractmethod
    def run(cls, ctx: Commander) -> None: ...


class BashGuardTool(GuardTool):
    @classmethod
    def run(cls, ctx: Commander) -> None:
        hook = ClaudeBashHook.read()
        session = CommanderSession(hook.session_id, ctx)
        try:
            reasons = GuardedCommand.refusals(
                hook.tool_input.command, session, hook.resolve_path(".")
            )
        except (ValueError, OSError) as error:
            hook.deny(f"Cannot resolve command safely: {error}")
            raise SystemExit(0) from error
        if reasons:
            hook.deny(" ".join(reasons))


class EditGuardTool(GuardTool, declared_name="edit-guard"):
    @classmethod
    def run(cls, ctx: Commander) -> None:
        hook = ClaudeFileEditHook.read()
        try:
            protected = SourceChangeCommand.protected(
                hook.resolve_path(hook.tool_input.file_path), ctx
            )
        except (ValueError, OSError) as error:
            hook.deny(f"Cannot resolve edit safely: {error}")
            raise SystemExit(0) from error
        if protected:
            reasons = CommanderSession(hook.session_id, ctx).refusals(SourceChangeCommand)
            if reasons:
                hook.deny(" ".join(reasons))


def main(a: list[str], ctx: Commander) -> None:
    tool = GuardTool.member_named(a[0]) if a else BashGuardTool
    tool.run(ctx)
