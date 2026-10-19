"""Claude's real compaction hooks observe the connected watch, never create one."""
from __future__ import annotations

from dataclasses import dataclass

from agent_comms_hooks import ClaudeHook, ClaudeHookProgram
from agent_comms_hooks.watch import WatchClaim


class BeforeCompaction(ClaudeHookProgram):
    events = ('PreCompact',)

    @classmethod
    def command(cls) -> str:
        return cls.console_script(f'{starting.__module__}:{starting.__name__}')


class AfterCompaction(ClaudeHookProgram):
    events = ('SessionStart',)

    @classmethod
    def command(cls) -> str:
        return cls.console_script(f'{finished.__module__}:{finished.__name__}')

    @classmethod
    def entry(cls) -> dict[str, object]:
        return {'matcher': 'compact', **super().entry()}


@dataclass(frozen=True)
class SessionStartHook(ClaudeHook):
    source: str


def starting() -> None:
    hook = ClaudeHook.read()
    from agent_comms.thread_status import CompactingThreadStatus

    WatchClaim(hook.session_id).state().observe_compaction(CompactingThreadStatus())


def finished() -> None:
    hook = SessionStartHook.read()
    if hook.source != 'compact':
        return
    from agent_comms.thread_status import ConnectedThreadStatus

    # SessionStart(source=compact) is Claude's first post-compaction observation,
    # including a manual /compact with no subsequent model turn or file update.
    WatchClaim(hook.session_id).state().observe_compaction(ConnectedThreadStatus())
