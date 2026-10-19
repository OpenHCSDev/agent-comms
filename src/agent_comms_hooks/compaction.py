"""Claude's real compaction hooks observe the connected watch, never create one."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from agent_comms_hooks import ClaudeHook, ClaudeHookProgram, HookValue
from agent_comms_hooks.watch import WatchClaim


class BeforeCompaction(ClaudeHookProgram):
    events = ("PreCompact",)

    @classmethod
    def command(cls) -> str:
        return cls.console_script(f"{starting.__module__}:{starting.__name__}")


class AfterCompaction(ClaudeHookProgram):
    events = ("SessionStart",)

    @classmethod
    def command(cls) -> str:
        return cls.console_script(f"{finished.__module__}:{finished.__name__}")

    @classmethod
    def entry(cls) -> dict[str, object]:
        return {"matcher": CompactedSource.name, **super().entry()}


class SessionSource(HookValue):
    @classmethod
    def decode(cls, value: object) -> SessionSource:
        for member in cls.__subclasses__():
            if member.name == value:
                return member()
        return OtherSource()

    def observe(self, claim: WatchClaim) -> None:
        """Startup, resume and future non-compaction sources do not end compaction."""


class OtherSource(SessionSource):
    name: ClassVar[str] = ""


class CompactedSource(SessionSource):
    name: ClassVar[str] = "compact"

    def observe(self, claim: WatchClaim) -> None:
        from agent_comms.thread_status import ConnectedThreadStatus

        claim.state().observe_compaction(ConnectedThreadStatus())


@dataclass(frozen=True)
class SessionStartHook(ClaudeHook):
    source: SessionSource


def starting() -> None:
    hook = ClaudeHook.read()
    from agent_comms.thread_status import CompactingThreadStatus

    WatchClaim(hook.session_id).state().observe_compaction(CompactingThreadStatus())


def finished() -> None:
    hook = SessionStartHook.read()
    hook.source.observe(WatchClaim(hook.session_id))
