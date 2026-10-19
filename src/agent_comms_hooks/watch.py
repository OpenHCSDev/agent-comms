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
from datetime import datetime, timedelta
from os import fstat
from pathlib import Path
from typing import TYPE_CHECKING

from agent_comms_hooks import ClaudeHook, ClaudeHookProgram

if TYPE_CHECKING:
    from agent_comms.commander.config import CommanderConfig


@dataclass(frozen=True)
class WatchRecord:
    bus: str
    root: str
    command: Sequence[str]

    def __post_init__(self) -> None:
        object.__setattr__(self, 'command', tuple(self.command))


@dataclass(frozen=True)
class WatchClaim:
    thread: str

    @property
    def path(self) -> Path:
        key = hashlib.sha256(self.thread.encode()).hexdigest()
        return Path.home() / ".local/state/agent-comms/cli-watch" / f"{key}.lock"

    @property
    def socket_path(self) -> Path:
        # sockaddr_un is short. Keep the locator derived from the session claim.
        return self.path.with_name(self.path.stem[:32] + '.sock')

    @property
    def reader_path(self) -> Path:
        return self.path.with_suffix('.reader.lock')

    @contextmanager
    def launching(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.path.with_suffix('.launch.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    @contextmanager
    def reader_held(self) -> Iterator[None]:
        with self.reader_path.open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield

    def has_reader(self) -> bool:
        if not self.reader_path.exists():
            return False
        with self.reader_path.open() as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
        return False

    def enrol(self, record: WatchRecord) -> None:
        with self.launching():
            current = self.state()
            if isinstance(current, LiveWatch):
                if current.record != record:
                    raise ValueError('This session is already bound to another bridge command')
                return
            with self.held(record):
                pass

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
                return LiveWatch(self, WatchRecord(**json.load(lock)))
            text = lock.read()
        if not text:
            return UnclaimedWatch()
        return LapsedWatch(self, WatchRecord(**json.loads(text)))


class WatchState:
    def report(self, hook: ClaudeHook) -> None:
        """Silent unless the conversation is meant to be watched and is not."""


class UnclaimedWatch(WatchState):
    pass


@dataclass(frozen=True)
class RecordedWatch(WatchState):
    claim: WatchClaim
    record: WatchRecord


class LiveWatch(RecordedWatch):
    def report(self, hook: ClaudeHook) -> None:
        if not self.claim.has_reader():
            hook.warn(
                f'agent-comms: the session bridge for {self.record.bus} is connected, but no '
                f'Monitor reader is running. Delivery is paused. Attach Monitor now: '
                f'{shlex.join(self.record.command)}')


class LapsedWatch(RecordedWatch):

    @property
    def warning(self) -> str:
        bus = self.record.bus
        return (
            f"agent-comms: the watch for bus thread {bus} (Core {self.record.root}) is not "
            f"running, so messages to {bus} are not reaching this session. Re-arm it now: "
            f"{shlex.join(self.record.command)}  "
            f"(to stop watching on purpose, remove {self.claim.path})"
        )

    def report(self, hook: ClaudeHook) -> None:
        hook.warn(self.warning)


class WatchGuard(ClaudeHookProgram):
    """Tells the session, on each prompt and tool result, that its bridge watch lapsed."""

    events = ("UserPromptSubmit", "PostToolUse")

    @classmethod
    def command(cls) -> str:
        return cls.console_script(f"{main.__module__}:{main.__name__}")


class PeerSync(ClaudeHookProgram):
    """Removed in the same PR that lands P216's shared order and handoff record."""

    events = ("UserPromptSubmit", "PostToolUse", "Stop")
    commanders = frozenset({"claude-code", "desktop-commander"})
    verb = "peer-sync"
    interval = timedelta(minutes=15)
    warning = (
        "Peer sync overdue: send the other commander your PRs needed, PRs held and any state "
        "disagreement over the relay now."
    )

    @classmethod
    def command(cls) -> str:
        return f"agent-comms-commander order {cls.verb}"

    @classmethod
    def sent(cls, config: CommanderConfig, target: str) -> None:
        """Record only a completed relay to this commander's counterpart."""
        if config.commander not in cls.commanders or target not in cls.commanders - {
            config.commander
        }:
            return
        path = config.peer_sync_log
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as log:
            fcntl.flock(log, fcntl.LOCK_EX)
            log.write(f"{datetime.now().astimezone():%H:%M} {target}\n")
            log.flush()

    @classmethod
    def overdue(cls, config: CommanderConfig) -> bool:
        """The append's filesystem clock supplies its date; HH:MM alone cannot."""
        try:
            with config.peer_sync_log.open() as log:
                fcntl.flock(log, fcntl.LOCK_SH)
                lines = log.readlines()
                written = datetime.fromtimestamp(fstat(log.fileno()).st_mtime).astimezone()
        except FileNotFoundError:
            return True
        peers = cls.commanders - {config.commander}
        if not lines or lines[-1].split() != [written.strftime("%H:%M"), *sorted(peers)]:
            return True
        elapsed = datetime.now().astimezone() - written
        return elapsed < timedelta() or elapsed >= cls.interval

    @classmethod
    def report(cls, hook: ClaudeHook, config: CommanderConfig) -> None:
        if cls.overdue(config):
            hook.warn(cls.warning)


def main() -> None:
    """Claude Code hook: warn the session whose bridge watch lapsed.

    A failure exits nonzero, which Claude Code reports without blocking the session."""
    hook = ClaudeHook.read()
    WatchClaim(hook.session_id).state().report(hook)
