"""The worker hosts a test launched, found by the bus root they were started on."""

from __future__ import annotations

from pathlib import Path

from agent_comms.execution.child_process import (
    ObservedProcess,
    Platform,
    ProcessIdentity,
)


class TestWorkerHosts:
    """Every worker host whose `--comms-root` lies under one test's temporary directory.

    A host is launched as `... --comms-root ROOT`, so the directory names all of them,
    including one a failed restart spawned but never recorded in the registry. The
    test's own teardown stops them; no fixture or test keeps its own partial list.
    """

    def __init__(self, directory: Path):
        self.directory = directory.resolve()

    def live(self) -> tuple[ProcessIdentity, ...]:
        platform = Platform.current()
        hosts = []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                identity = platform.identity(int(entry.name))
                arguments = platform.arguments(identity)
            except ProcessLookupError:  # exited during the scan: not live
                continue
            if any(flag == "--comms-root" and Path(value).resolve().is_relative_to(self.directory)
                   for flag, value in zip(arguments, arguments[1:])):
                hosts.append(identity)
        return tuple(hosts)

    def stop(self) -> None:
        for identity in self.live():
            ObservedProcess(identity).stop_sync()
