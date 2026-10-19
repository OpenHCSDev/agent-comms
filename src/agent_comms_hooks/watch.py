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
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_comms.thread_status import ThreadStatus
    from agent_comms_hooks.session import SessionHook

from agent_comms_hooks import ClaudeHook, ClaudeHookProgram


@dataclass(frozen=True)
class WatchRecord:
    bus: str
    root: str
    command: Sequence[str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "command", tuple(self.command))


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
        return self.path.with_name(self.path.stem[:32] + ".sock")

    @property
    def reader_path(self) -> Path:
        return self.path.with_suffix(".reader.lock")

    @contextmanager
    def launching(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.path.with_suffix(".launch.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    @contextmanager
    def reader_held(self) -> Iterator[None]:
        with self.reader_path.open("a") as lock:
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
            self.state().enrol(self, record)

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
    def enrol(self, claim: WatchClaim, record: WatchRecord) -> None:
        with claim.held(record):
            pass

    def start_session(self, hook: SessionHook) -> None:
        """A session without a recorded bridge never acquires one from a hook."""

    def end_session(self, hook: SessionHook) -> None:
        """An absent or lapsed bridge has nothing to stop."""

    def observe_compaction(self, status: ThreadStatus) -> None:
        """An absent watch never acquires bridge presence from a hook."""

    def report(self, event: str) -> None:
        """Silent unless the conversation is meant to be watched and is not."""


class UnclaimedWatch(WatchState):
    pass


@dataclass(frozen=True)
class RecordedWatch(WatchState):
    claim: WatchClaim
    record: WatchRecord

    def start_session(self, hook: SessionHook) -> None:
        hook.start_recorded(self)

    def require_launch(self, error: OSError) -> None:
        """A lapsed record may launch its bridge once under the launch lock."""

    def report_warning(self, event: str, warning: str) -> None:
        context = {"hookEventName": event, "additionalContext": warning}
        print(json.dumps({"hookSpecificOutput": context}))


class LiveWatch(RecordedWatch):
    def enrol(self, claim: WatchClaim, record: WatchRecord) -> None:
        if self.record != record:
            raise ValueError("This session is already bound to another bridge command")

    def require_launch(self, error: OSError) -> None:
        raise RuntimeError(
            "An older watch still owns this session. Stop that Monitor once, then connect."
        ) from error

    def end_session(self, hook: SessionHook) -> None:
        hook.end_recorded(self)

    def report(self, event: str) -> None:
        if not self.claim.has_reader():
            self.report_warning(
                event,
                f"agent-comms: the session bridge for {self.record.bus} is connected, but no "
                f"Monitor reader is running. Delivery is paused. Attach Monitor now: "
                f"{shlex.join(self.record.command)}",
            )

    def observe_compaction(self, status: ThreadStatus) -> None:
        # Hooks on every prompt keep their stdlib-only import path. Only an
        # actual compaction observation needs the registry transaction owner.
        from agent_comms.registration import Registration

        registry = Registration(Path(self.record.root) / "registry.json")
        original = registry.entry(self.record.bus).entry
        # The watch must still be the one read before importing Core. Its claim
        # is held inside its registered endpoint lifetime; the transaction then
        # fences that exact incarnation, generation and process, not a name.
        if self.claim.state() == self:
            registry.observe_external_status(
                original.owner_identity, original.thread.require_process(), status
            )


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

    def report(self, event: str) -> None:
        self.report_warning(event, self.warning)


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
    WatchClaim(hook.session_id).state().report(hook.hook_event_name)
