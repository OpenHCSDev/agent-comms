"""The watch claim of one CLI conversation, readable without importing agent_comms.

A bridge watch holds an exclusive flock on its claim for its whole life and
writes there the command that started it. The kernel drops the lock when the
process dies, so a claim with a record and no holder is a watch that lapsed.
Claude Code runs ``agent-comms-watch-guard`` on every prompt and tool result;
importing agent_comms takes a second, so this module is stdlib-only.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import shlex
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields
from pathlib import Path


@dataclass(frozen=True)
class WatchRecord:
    bus: str
    root: str
    command: Sequence[str]


@dataclass(frozen=True)
class WatchClaim:
    thread: str

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
        return LapsedWatch(self, WatchRecord(**json.loads(text)))


class WatchState:
    def report(self, event: str) -> None:
        """Silent unless the conversation is meant to be watched and is not."""


class UnclaimedWatch(WatchState):
    pass


class LiveWatch(WatchState):
    pass


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
        context = {"hookEventName": event, "additionalContext": self.warning}
        print(json.dumps({"hookSpecificOutput": context}))


@dataclass(frozen=True)
class ClaudeHook:
    session_id: str
    hook_event_name: str

    @classmethod
    def read(cls) -> ClaudeHook:
        hook = json.load(sys.stdin)
        return cls(**{declared.name: hook[declared.name] for declared in fields(cls)})


def main() -> None:
    """Claude Code hook: warn the session whose bridge watch lapsed."""
    try:
        hook = ClaudeHook.read()
        WatchClaim(hook.session_id).state().report(hook.hook_event_name)
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"agent-comms-watch-guard: {type(error).__name__}: {error}")
