"""Install units and their retained diagnostics, without loading the install runner."""

from __future__ import annotations

import glob
import json
import shutil
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from agent_comms.child_process import ResidentManager
from agent_comms.field_codec import FieldCodec, Id128
from agent_comms.installer.backup import StoreBackup
from agent_comms.installer.refusals import UpgradeRefused
from agent_comms.repository_name import RepositoryName
from agent_comms_hooks.process_claim import (
    ClaimHeldError,
    ProcessSingleton,
)


@dataclass(frozen=True)
class InstallCause:
    """The repository merge that caused an automatic install, independent of its target."""

    repo: RepositoryName
    main: str


@dataclass(frozen=True)
class InstallUnit:
    """One install as a transient user unit, named by its diagnostics directory.

    Systemd owns its lifetime exactly as it owns the connector's, so an install the heartbeat
    starts from inside the connector's unit survives stopping that unit.
    """

    identity: Id128
    logs: Path
    inherited: ClassVar[tuple[str, ...]] = (
        "HOME", "PATH", "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME",
        "XDG_CACHE_HOME", "AGENT_COMMS_DEBUG_LOG",
    )
    """The caller's variables the install reads, each copied by the manager that runs it."""

    @property
    def unit(self) -> str:
        return f"agent-comms-install-{self.identity}.service"

    @property
    def result(self) -> Path:
        return self.logs / "result.json"

    @property
    def errors(self) -> Path:
        return self.logs / "stderr.log"

    @property
    def request(self) -> Path:
        """The ``InstallRun`` the unit reads on its standard input, as every runtime's launcher
        has sent it: the launcher is the installed runtime, older than the tree it starts."""
        return self.logs / "request.json"

    def start(self, tree: Path, command: tuple[str, ...], pythonpath: str, *,
              wait: bool) -> None:
        """Start the unit; ``wait`` follows it to its end and raises on its failure."""
        ResidentManager.current().transient(
            self.unit, command, cwd=tree, stdin=self.request, stdout=self.result,
            stderr=self.errors, inherited=self.inherited, assigned={"PYTHONPATH": pythonpath},
            wait=wait)

    def running(self) -> bool:
        return not ResidentManager.current().activity(self.unit).ended()

    def stop(self) -> None:
        """Systemd signals the install, which runs its abort, and returns once the unit ends."""
        ResidentManager.current().control("stop", self.unit)

    def died(self) -> bool:
        """A unit gone without printing a result died; any other was launched."""
        return (ResidentManager.current().activity(self.unit).ended()
                and not self.result.read_text())

    def last_error(self) -> str:
        return next((line for line in reversed(self.errors.read_text().splitlines())
                     if line.strip()), "no output")


class InstallRunning(UpgradeRefused):
    """One install per bus root: a launch that finds another one launching or running
    refuses and names the running units, so a heartbeat can count the merge as launched."""

    def __init__(self, lock: Path, units: tuple[InstallUnit, ...]) -> None:
        self.units = units
        super().__init__(f"another install holds {lock}; running units: "
                         f"{', '.join(unit.unit for unit in units)}")


@dataclass(frozen=True)
class InstallLogs:
    """Each install's diagnostics beside the bus, kept as long as its data backups are."""

    root: Path
    cause_file: ClassVar[str] = "cause.json"

    @property
    def launch_claim(self) -> ProcessSingleton:
        return ProcessSingleton(self.root.parent / f".{self.root.name}.install.lock")

    @contextmanager
    def launching(self) -> Iterator[None]:
        """Held from the target checkout to the unit's start, and for a waiting launch to the
        unit's end; under it no other unit runs, so minting and retiring never race one."""
        with ExitStack() as held:
            try:
                held.enter_context(self.launch_claim.claimed())
            except ClaimHeldError:
                raise InstallRunning(self.launch_claim.path, self.running()) from None
            if running := self.running():
                raise InstallRunning(self.launch_claim.path, running)
            yield

    def units(self) -> tuple[InstallUnit, ...]:
        return tuple(
            InstallUnit(Id128(path.name.removeprefix(self.prefix)), path)
            for path in self.root.parent.glob(f"{glob.escape(self.prefix)}*") if path.is_dir()
        )

    def running(self) -> tuple[InstallUnit, ...]:
        return tuple(unit for unit in self.units() if unit.running())

    def stop_running(self) -> None:
        """Systemd, not their caller, owns the root's installs, so whoever removes the root
        stops them first; a caller killed mid-install leaves its unit writing otherwise."""
        for unit in self.running():
            unit.stop()

    def launched(self, repo: RepositoryName, main: str) -> tuple[InstallUnit, ...]:
        """The runs launched for this merge, decoded once from their launch records."""
        cause = InstallCause(repo, main)
        return tuple(
            unit for unit in self.units()
            if cause in (FieldCodec.decode(InstallCause, FieldCodec.parse(marker.read_text()))
                         for marker in unit.logs.glob("cause*.json"))
        )

    def record(self, logs: Path, repo: RepositoryName, main: str) -> None:
        """A run installs every repository's merge at once. Its first cause keeps the name
        every runtime reads, and each further repository's merge gets its own file."""
        marker = logs / self.cause_file
        if marker.exists():
            marker = logs / f"cause-{Id128.mint()}.json"
        marker.write_text(json.dumps(FieldCodec.encode(InstallCause(repo, main))))

    @property
    def prefix(self) -> str:
        return f".{self.root.name}.installer-"

    def minted(self) -> InstallUnit:
        """A new install's own unit and directory; older ones past the backups' count are
        retired."""
        unit = InstallUnit(identity := Id128.mint(), self.root.parent / f"{self.prefix}{identity}")
        unit.logs.mkdir(mode=0o700)
        self.retire()
        return unit

    def retire(self) -> tuple[Path, ...]:
        """The names carry no time, so age is each directory's own modification time; a
        running unit's directory is its live output and is never retired."""
        logs = sorted((unit.logs for unit in self.units() if not unit.running()),
                      key=lambda path: path.stat().st_mtime)
        retired = tuple(logs[:-StoreBackup.kept])
        for path in retired:
            shutil.rmtree(path)
        return retired
