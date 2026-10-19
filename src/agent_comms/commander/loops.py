"""One process owns the commander's independent, re-armed monitor threads."""
from __future__ import annotations

import argparse
import sys
import threading
import time
import traceback
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from io import TextIOBase
from pathlib import Path
from typing import ClassVar, TextIO

from agent_comms_hooks.watch import LoopClaim, LoopsClaim, LoopsRecord, WatchRecord

from ..cli_bridges.claude import ClaudeBridge
from ..cli_bridges.shared import Binding, Options, Watch, run_bridge


class ThreadDestination(threading.local):
    def __init__(self, output: TextIO | TextIOBase) -> None:
        self.output = output


class ThreadOutput(TextIOBase):
    """Route print calls by their executing thread, never by a global redirect."""

    def __init__(self, output: TextIO) -> None:
        self.output = output
        self.local = ThreadDestination(output)

    def write(self, text: str) -> int:
        return self.local.output.write(text)

    def flush(self) -> None:
        self.local.output.flush()

    @contextmanager
    def using(self, output: TextIO | TextIOBase) -> Iterator[None]:
        previous, self.local.output = self.local.output, output
        try:
            yield
        finally:
            self.flush()
            self.local.output = previous


class Loop(TextIOBase, ABC):
    """A registered loop owns its thread, output framing and lifetime claim."""

    members: ClassVar[dict[str, type[Loop]]] = {}
    output_lock: ClassVar[threading.Lock] = threading.Lock()
    name: ClassVar[str]

    def __init_subclass__(cls, *, name: str) -> None:
        super().__init_subclass__()
        cls.name = name
        cls.members[name] = cls

    def __init__(self) -> None:
        super().__init__()
        self.output = sys.stdout
        self.pending = ""
        self.thread = threading.Thread()

    @abstractmethod
    def run(self) -> None: ...

    def configure(self, state: Path) -> None:
        """Only loops with a separate input journal need this transport setting."""

    def write(self, text: str) -> int:
        self.pending += text
        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            with self.output_lock:
                stamp = datetime.now().isoformat(timespec="seconds")
                self.output.write(f"[{self.name} {stamp}] {line}\n")
                self.output.flush()
        return len(text)

    def flush(self) -> None:
        if self.pending:
            self.write("\n")

    def start(self, session: str, router: ThreadOutput, record: WatchRecord) -> None:
        if self.thread.is_alive():
            return
        self.thread = threading.Thread(
            target=self._run, args=(session, router, record),
            name=f"commander-{self.name}", daemon=True,
        )
        self.thread.start()

    def _run(self, session: str, router: ThreadOutput, record: WatchRecord) -> None:
        with router.using(self):
            try:
                with LoopClaim(session, self.name).held(record):
                    print("running", flush=True)
                    self.run()
            except BaseException:
                # A monitor's failure is output, not permission to stop its peers.
                traceback.print_exc(file=self)


class InboxLoop(Loop, name="inbox"):
    @staticmethod
    def default_state() -> Path:
        return Path.home() / ".local/state/claude-comms-inbox"

    def configure(self, state: Path) -> None:
        self.state = state

    def run(self) -> None:
        # The existing bridge journal owns attempted inputs, including UNKNOWN.
        run_bridge(ClaudeBridge, Options(Watch(), None, self.state))


def main(arguments: list[str]) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path,
                        default=InboxLoop.default_state())
    parser.add_argument("--tick", type=float, default=2.0)
    options = parser.parse_args(arguments)
    if options.tick <= 0:
        parser.error("--tick must be positive")
    with Binding.open(options.state) as db:
        binding = Binding.bind(db, None)
    command = ("agent-comms-commander", "loops", "--state", str(options.state))
    record = WatchRecord(binding.bus, binding.root, command)
    loops = [member() for member in Loop.members.values()]
    for loop in loops:
        loop.configure(options.state)
    group = LoopsRecord(binding.bus, binding.root, command, tuple(Loop.members))
    output, errors = sys.stdout, sys.stderr
    router = ThreadOutput(output)
    sys.stdout = sys.stderr = router
    try:
        with router.using(output), LoopsClaim(binding.thread).held(group):
            while True:
                for loop in loops:
                    loop.start(binding.thread, router, record)
                time.sleep(options.tick)
    finally:
        sys.stdout, sys.stderr = output, errors
