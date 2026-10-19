"""The watch claim of one CLI conversation, and the hook that reports a lapsed one.

A bridge watch holds an exclusive flock on its claim for its whole life and
writes there the command that started it. The kernel drops the lock when the
process dies, so a claim with a record and no holder is a watch that lapsed.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import shlex
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import ClassVar

from agent_comms_hooks import ClaudeHook, ClaudeHookProgram, HookRecord


@dataclass(frozen=True)
class WatchRecord(HookRecord):
    bus: str
    root: str
    command: Sequence[str]


@dataclass(frozen=True)
class WatchClaim:
    thread: str
    record_type: ClassVar[type[WatchRecord]] = WatchRecord

    @property
    def path(self) -> Path:
        key = hashlib.sha256(self.thread.encode()).hexdigest()
        return Path.home() / ".local/state/agent-comms/cli-watch" / f"{key}.lock"

    @contextmanager
    def held(self, record: WatchRecord) -> Iterator[None]:
        """One watch per conversation, independent of --state and bus aliases."""
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            lock.truncate(0)
            lock.write(json.dumps(asdict(record)))
            lock.flush()
            yield

    def state(self) -> WatchState:
        if not self.path.exists():
            return UnclaimedWatch()
        with self.path.open() as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return LiveWatch()
            text = lock.read()
        if not text:
            return UnclaimedWatch()
        return LapsedWatch(self, self.record_type.decode(json.loads(text)))


class WatchState:
    running = False

    @staticmethod
    def emit(event: str, warning: str) -> None:
        context = {"hookEventName": event, "additionalContext": warning}
        print(json.dumps({"hookSpecificOutput": context}))

    def report(self, event: str) -> None:
        """Silent unless the conversation is meant to be watched and is not."""


class UnclaimedWatch(WatchState):
    pass


class LiveWatch(WatchState):
    running = True


@dataclass(frozen=True)
class LapsedWatch(WatchState):
    claim: WatchClaim
    record: WatchRecord

    @property
    def warning(self) -> str:
        bus = self.record.bus
        return (
            f"agent-comms: the watch for bus thread {bus} (Core {self.record.root}) is not "
            f"running, so messages to {bus} are not reaching this session. Re-arm it now: "
            f"{shlex.join(self.record.command)}  "
            f"(to stop watching on purpose, remove {self.claim.path})"
        )

    def report(self, event: str) -> None:
        self.emit(event, self.warning)


@dataclass(frozen=True)
class LoopsRecord(WatchRecord):
    loops: Sequence[str]


@dataclass(frozen=True)
class LoopClaim(WatchClaim):
    name: str

    @property
    def path(self) -> Path:
        path = super().path
        return path.parent / f"{path.stem}.loops" / f"{self.name}.lock"


@dataclass(frozen=True)
class LoopsClaim(WatchClaim):
    record_type: ClassVar[type[WatchRecord]] = LoopsRecord

    @property
    def path(self) -> Path:
        return super().path.with_suffix(".loops.json")

    def report(self, event: str) -> bool:
        if not self.path.exists():
            return False
        record = LoopsRecord.decode(json.loads(self.path.read_text()))
        missing = [name for name in record.loops
                   if not LoopClaim(self.thread, name).state().running]
        if missing:
            WatchState.emit(event,
                f"agent-comms: commander loops not running: {', '.join(missing)} "
                f"(bus {record.bus}, Core {record.root}). Re-arm: {shlex.join(record.command)}")
        return True


class WatchGuard(ClaudeHookProgram):
    """Tells the session, on each prompt and tool result, that its bridge watch lapsed."""

    events = ("UserPromptSubmit", "PostToolUse")

    @classmethod
    def command(cls) -> str:
        return cls.console_script(f"{main.__module__}:{main.__name__}")


def main() -> None:
    """Claude Code hook: warn the session whose bridge watch lapsed.

    A failure exits nonzero, which Claude Code reports without blocking the session."""
    hook = ClaudeHook.read()
    if not LoopsClaim(hook.session_id).report(hook.hook_event_name):
        WatchClaim(hook.session_id).state().report(hook.hook_event_name)
