"""Upgrade a retained bus and the checkout that runs it: what ``agent-comms install`` runs.

``agent-comms install`` checks out the target revision and runs this package from that
revision's ``src`` (``python -m agent_comms.installer``), so its steps are the
target's. The target's stack is built into its own runtime, relocked to the latest
OpenHCSDev mains, and must import Toad and the ACP client. Each carry step's dry run
on the bus says whether it is pending; each runs with the target's
``src`` and ``tools`` (``agent_comms.installer.carry``). Every run first rehearses on a
copy of the bus: the steps, then ``who``, ``threads`` and ``history`` reads, and
every file no step names as rewritten must be unchanged by saved-file size,
nanosecond mtime and inode, and the installed runtime answers the owner probe the live
run asks before its stop. ``--rehearse`` stops there.

A live run then stops the running agent owners with the installed CLI, backs up only
files the rehearsal changed, moves the checkout, prepares native Pi, carries the bus,
reads it back, publishes the built runtime and starts only the owners it stopped. A
failure after the backup restores the original runtime and data. No saved input is
replayed: owners start by the normal start path. The installer reports its
start and result through the bus's ordinary publisher.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, ClassVar, Self

from pydantic import TypeAdapter

from agent_comms import cli
from agent_comms.active_route import ActiveRoute, active_route_path, route_new_bus
from agent_comms.bus import Bus
from agent_comms.cli_bridges.shared import Serve, SessionBridgeResumes
from agent_comms.cli_commands import (
    BusReadBack,
    CliCommand,
    InstallResult,
    PlannedReconcileCliCommand,
    ReadBackCliCommand,
    ReconcileStoresCliCommand,
    StartCliCommand,
    StopCliCommand,
)
from agent_comms.cli_protocol import CoreError
from agent_comms.cli_result import CliResult
from agent_comms.comms import Comms
from agent_comms.coordination_errors import CoordinationReadUnavailable
from agent_comms.declared_family import DeclaredFamily
from agent_comms.field_codec import FieldCodec, MalformedRecord, PathText
from agent_comms.host_services import HostService, RestartedService
from agent_comms.install_choices import InstallerChoice
from agent_comms.installer import publication, step_worker
from agent_comms.installer.backup import (
    BackupReport,
    RestoreSpace,
    ResourceBackup,
    SavedFile,
    StoreBackup,
    UpgradeRefused,
    bus_files,
    transcripts_held,
)
from agent_comms.installer.carry import (
    STEPS,
    BackupFilesOperation,
    Carried,
    Carry,
    CarryChain,
    CarryOperation,
    MigrateOperation,
    RetireInputsOperation,
)
from agent_comms.installer.practice import (
    PracticeMeasurement,
    RehearsalHome,
    RuntimeHome,
    practice_copy,
)
from agent_comms.installer.commands import InstallCommands, TimedCommand
from agent_comms.installer.interpreter_operation import (
    ClientImportsProbe,
    PackageRootProbe,
    PinnedLocationProbe,
    PrepareNativeProbe,
    RunningOwnersProbe,
    RuntimeProbe,
)
from agent_comms.installer.runtime_resolution import RuntimeResolution
from agent_comms.launch_runtime import LaunchRuntime, RestartEnvironment, SpecifiedImportPath
from agent_comms.maintenance_barrier import MaintenanceBarrier, MaintenanceOperator, MaintenanceState
from agent_comms.locked_store import BusFile, RootFile
from agent_comms.registry_store import RegistryStore
from agent_comms.repo import Checkout, ScratchCheckout
from agent_comms.storage_rules import StorageRule
from agent_comms.recorded_bus import RecordedBusIdentity

if TYPE_CHECKING:
    from agent_comms.installer.launch import InstallReport

HOME = Path.home()
CORE_ERROR = TypeAdapter(CoreError.declared_union())


@dataclass(frozen=True)
class StepRun:
    """One step's run on one bus."""

    carry: str
    carried: Carried


@dataclass(frozen=True)
class RehearsalFiles:
    """The rehearsal's named rewrite set and the restore reservation it requires."""

    identical: int
    rewritten: int
    names: tuple[str, ...]
    restore_bytes: int

    @classmethod
    def none(cls) -> RehearsalFiles:
        """The files of a rehearsal that rewrote nothing."""
        return cls(0, 0, (), 0)

    @staticmethod
    def declared(steps: tuple[StepRun, ...]) -> frozenset[str]:
        return frozenset(name for step in steps for name in step.carried.rewritten)

    def require_rehearsal(self, rehearsal: RehearsalFiles) -> None:
        outside = sorted(set(self.names) - set(rehearsal.names))
        if outside:
            raise UpgradeRefused(f"Live carries changed files absent from rehearsal: {outside}")



def carried(root: Path, before: dict[str, SavedFile], steps: tuple[StepRun, ...],
            expected: frozenset[str]) -> RehearsalFiles:
    """Only the files a step names as rewritten may differ from ``before``."""
    after = bus_files(root)
    physical = {name for name in before.keys() | after.keys()
                if before.get(name) != after.get(name)}
    # SQLite's committed WAL belongs to its database declaration, not a second carry.
    changed = {name[:-4] if name.endswith('-wal') and name[:-4] in before.keys() | after.keys()
               else name for name in physical}
    unnamed = sorted(changed - RehearsalFiles.declared(steps))
    if unnamed:
        raise UpgradeRefused(f"Carries changed files no step names: {unnamed}")
    outside = sorted(changed - expected)
    if outside:
        raise UpgradeRefused(f"Carries changed files outside the expected set: {outside}")
    return RehearsalFiles(
        identical=len(before.keys() - changed), rewritten=len(changed),
        names=tuple(sorted(changed)),
        restore_bytes=sum(before[name].size for name in physical & before.keys()))


@dataclass(frozen=True)
class OwnerStart(DeclaredFamily, affix="Owner"):
    """What starting one stopped owner did. A unit that cannot start is reported by name;
    the runtime and bus it would run on are installed either way."""

    name: str

    @abstractmethod
    def lines(self) -> tuple[str, ...]: ...

    @staticmethod
    def attempt(stack: Stack, root: Path, name: str) -> OwnerStart:
        try:
            stack.cli(root, StartCliCommand, "--name", name)
        except UpgradeRefused as error:
            return RefusedOwner(name, str(error))
        return StartedOwner(name)


@dataclass(frozen=True)
class StartedOwner(OwnerStart):
    def lines(self) -> tuple[str, ...]:
        return (f"Started owner: {self.name}",)


@dataclass(frozen=True)
class RefusedOwner(OwnerStart):
    error: str

    def lines(self) -> tuple[str, ...]:
        return (f"Owner not started: {self.name}: {self.error}",)


class InstallRolledBack(UpgradeRefused):
    """The original installation was restored after a failed live pass."""

    def __init__(self, error: BaseException, started: tuple[OwnerStart, ...], backup: str,
                 *, runtime: LaunchRuntime):
        self.error = error
        self.started = started
        self.backup = backup
        self.runtime = runtime
        super().__init__("; ".join(self.lines()))

    def lines(self) -> tuple[str, ...]:
        return (
            f"Install rolled back: {type(self.error).__name__}: {self.error}",
            f"Original runtime selected again: {self.runtime.prefix}",
            *(line for owner in self.started for line in owner.lines()),
            f"Backup: {self.backup}",
        )


class Extendable:
    """A record a subclass extends with facts of its own; the shared ones carry over."""

    def extended[Record](self, kind: type[Record], **facts: object) -> Record:
        return kind(**{each.name: getattr(self, each.name) for each in fields(self)}, **facts)


@dataclass(frozen=True)
class CarriedPass(Extendable):
    """What a rehearsal and a live install share: the steps, the files they changed, and the
    bus read back."""

    steps: tuple[StepRun, ...]
    files: RehearsalFiles
    read: BusReadBack
    retired_inputs: tuple[StepRun, ...] = field(default=(), kw_only=True)

    def after_acceptance(self, pending: tuple[Carry, ...], stack: Stack,
                         tree: TargetTree) -> Self:
        return replace(self, retired_inputs=tuple(
            StepRun(step.name(), stack.call_step(step, tree, RetireInputsOperation()))
            for step in pending
        ))

    def require_read(self, carried_from: BusReadBack) -> None:
        """The bus reads back after its carry what it read before it, or the carry lost
        something on this root."""
        if self.read != carried_from:
            raise UpgradeRefused(
                f"The carried bus read back {self.read}; before its carry it read {carried_from}")


@dataclass(frozen=True)
class StoppedOwners:
    stopped: tuple[str, ...]

    def stopped_lines(self) -> tuple[str, ...]:
        return (f"Stopped owners: {', '.join(self.stopped) or 'none'}",)


@dataclass(frozen=True)
class RestartedServices:
    services: tuple[RestartedService, ...]

    def service_lines(self) -> tuple[str, ...]:
        return (f"Restarted services: "
                f"{', '.join(service.line() for service in self.services) or 'none'}",)


class Rehearsal(DeclaredFamily, affix="Rehearsal"):
    """What a run tried before it touched the live bus."""


class RehearsedFiles(Rehearsal, declared_name=None):
    """A rehearsal that names the files its live pass may rewrite."""

    files: RehearsalFiles


@dataclass(frozen=True)
class CarriedRehearsal(CarriedPass, RehearsedFiles):
    copy: PracticeMeasurement


@dataclass(frozen=True)
class UncopiedRehearsal(RehearsedFiles):
    """A read of the live root found no carry pending and no store to reconcile, so no
    practice copy was taken and the live pass may rewrite nothing. Its own planned
    reconcile, under the hold, still refuses before any write if it names a file."""

    files: RehearsalFiles = field(default_factory=RehearsalFiles.none)


@dataclass(frozen=True)
class UnchangedRehearsal(Rehearsal):
    """Nothing to rehearse: the checkout and lock are already the target's."""


@dataclass(frozen=True)
class FreshRehearsal(Rehearsal):
    """Nothing to rehearse: the target creates the bus."""


@dataclass(frozen=True)
class RetiringRehearsal(FreshRehearsal):
    """The bus at the root moves aside before a fresh one is created there."""


class LiveRun(DeclaredFamily, affix="Run"):
    """What a live install changed."""

    @abstractmethod
    def lines(self) -> tuple[str, ...]: ...


@dataclass(frozen=True)
class CarriedRun(CarriedPass, StoppedOwners, RestartedServices, LiveRun):
    started: tuple[OwnerStart, ...]
    restore_space: RestoreSpace
    backup: BackupReport
    retired_backups: tuple[Annotated[Path, PathText], ...]

    def lines(self) -> tuple[str, ...]:
        return (
            *(f"Carried {step.carry}: {step.carried.changed} changed" for step in self.steps),
            *self.stopped_lines(),
            *(line for owner in self.started for line in owner.lines()),
            *self.service_lines(),
            f"Backup: {self.backup.backup} ({self.backup.bytes} bytes)",
            *self.read.lines(),
        )


@dataclass(frozen=True)
class UnchangedRun(LiveRun):
    """Published the installed runtime again and planned the next install."""

    def lines(self) -> tuple[str, ...]:
        return ("Runtime republished; nothing carried",)


@dataclass(frozen=True)
class CreatedRun(RestartedServices, LiveRun, Extendable):
    route: str

    def lines(self) -> tuple[str, ...]:
        return (f"Created bus; route: {self.route}", *self.service_lines())


@dataclass(frozen=True)
class RetiredRun(CreatedRun, StoppedOwners):
    retired: Annotated[Path, PathText]

    def lines(self) -> tuple[str, ...]:
        return (*super().lines(), *self.stopped_lines(), f"Retired bus: {self.retired}")


@dataclass(frozen=True)
class Upgrade(DeclaredFamily, affix="Upgrade"):
    """The source, target and pending carries found by one install."""

    source: str
    target: str
    root: Annotated[Path, PathText]
    pending: tuple[str, ...]

    def lines(self) -> tuple[str, ...]:
        return (f"{self.source[:12]} -> {self.target[:12]}",
                f"Pending carries: {', '.join(self.pending) or 'none'}")


@dataclass(frozen=True)
class RehearsedUpgrade(Upgrade):
    rehearsal: Rehearsal
    commands: tuple[TimedCommand, ...] = field(kw_only=True)
    """Every command the install ran, in the order each finished, with its time."""

    def lines(self) -> tuple[str, ...]:
        slowest = sorted(self.commands, key=lambda timed: timed.seconds, reverse=True)[:5]
        return (*super().lines(),
                f"Commands: {len(self.commands)} in "
                f"{sum(timed.seconds for timed in self.commands):.1f}s; slowest:",
                *(timed.line() for timed in slowest))


@dataclass(frozen=True)
class InstalledUpgrade(RehearsedUpgrade, Extendable):
    live: LiveRun
    install: InstallResult
    """The harnesses registered on the bus the target now reads."""

    finished: ClassVar[str] = "Install finished"
    """The headline of the post that reports this upgrade."""

    def lines(self) -> tuple[str, ...]:
        return (*super().lines(), *self.live.lines())


@dataclass(frozen=True)
class UnresumedUpgrade(InstalledUpgrade):
    """Installed, with session bridges paused for the swap that did not come back: the
    install exits non-zero after reporting it."""

    unresumed: tuple[str, ...]

    finished: ClassVar[str] = "Install finished; session bridges not resumed"

    def lines(self) -> tuple[str, ...]:
        return (*super().lines(), *self.unresumed)


@dataclass(frozen=True)
class Stack:
    """One built installation and the source checkout containing its install tools."""

    checkout: Path
    commands: InstallCommands
    home: Path = HOME
    runtime: LaunchRuntime = field(default_factory=LaunchRuntime.installed)

    @property
    def python(self) -> Path:
        return self.runtime.interpreter

    @property
    def lock(self) -> Path:
        return self.checkout / "stack/uv.lock"

    @property
    def uv(self) -> Path:
        """The uv the installer runs, kept in the installer's own state."""
        return RuntimeHome.uv_path(self.home)

    def require_uv(self) -> None:
        if not os.access(self.uv, os.X_OK):
            raise UpgradeRefused(f"No uv executable at {self.uv}; the installer runs only that uv")

    @classmethod
    def build(cls, checkout: Path, commands: InstallCommands, home: Path = HOME) -> Stack:
        """Resolve current mains once, then build or reuse their immutable installation."""
        Checkout(checkout).prepare()
        source = cls(checkout, commands, home, runtime=LaunchRuntime.running())
        source.require_uv()
        commands.run(source.uv, "lock", "--project", "stack", "--upgrade", "--python", "3.14",
                     cwd=checkout)
        core = commands.run("git", "-C", checkout, "rev-parse", "HEAD").strip()
        resolution = hashlib.sha256(source.lock.read_bytes()).hexdigest()[:16]
        prefix = home / ".local/share/agent-comms/runtimes" / f"core-{core[:9]}-lock-{resolution}"
        built = replace(source, runtime=LaunchRuntime(prefix / "bin/python"))
        if prefix.exists():
            if not RuntimeResolution.at(prefix).matches(source.lock, built.python, core, commands):
                raise UpgradeRefused("Existing built runtime differs from its resolved inputs")
            return built
        prefix.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        prefix.mkdir(mode=0o700)
        try:
            commands.run(source.uv, "sync", "--project", "stack", "--locked", "--python", "3.14",
                         "--no-editable", cwd=checkout,
                         env={**built.environment(), "UV_PROJECT_ENVIRONMENT": str(prefix)})
            built.install_native()
            RuntimeResolution.capture(built.python, core, commands).save(prefix)
            built.imports()
        except BaseException:
            shutil.rmtree(prefix)
            raise
        return built

    def test(self, *pytest_arguments: str) -> None:
        """Run checkout tests against this runtime's installed packages, never its source."""
        self.commands.run(self.uv, "pip", "install", "--python", self.python,
                          "-r", "pyproject.toml", "--extra", "dev", cwd=self.checkout,
                          env=self.environment())
        core = self.commands.run("git", "-C", self.checkout, "rev-parse", "HEAD").strip()
        scratch = StorageRule().scratch_base
        scratch.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch, prefix=f"tests-{core[:12]}-") as temporary:
            print(self.commands.run(
                "timeout", "300", self.python, "-m", "pytest", "-n", "0", "-q", "-s",
                "-o", "pythonpath=. tools", "-o", "faulthandler_timeout=60",
                "--basetemp", Path(temporary) / "pytest", "-p", "no:cacheprovider",
                *pytest_arguments, cwd=self.checkout, env=self.environment()), end="")

    def probe[Answer](self, probe: RuntimeProbe[Answer], *,
                      source: tuple[Path, ...] = ()) -> Answer:
        return self.commands.probe(probe, self.python, cwd=self.checkout,
                                   env=self.environment(*source))

    def imports(self) -> None:
        self.probe(ClientImportsProbe())

    def install_native(self) -> None:
        """Build, copy and qualify this interpreter's native artifact before publication."""
        self.commands.run(self.checkout / "stack/bin/prepare-pi-native", "--python", self.python,
                          cwd=self.checkout, env=self.environment(self.checkout / "src"))
        native = self.probe(PinnedLocationProbe(), source=(self.checkout / "src",))
        package = self.probe(PackageRootProbe())
        destination = package / "_native" / native.relative_to(self.checkout / "stack")
        destination.parents[2].mkdir(mode=0o700, parents=True)
        shutil.copytree(native, destination)
        self.prepare_native()

    def prepare_native(self) -> None:
        self.probe(PrepareNativeProbe())

    def publish(self) -> None:
        """The selected interpreter owns console metadata and harness declarations."""
        self.commands.run(
                          self.python, "-m", publication.__name__,
                          cwd=self.checkout, env=self.environment(),
        )

    def environment(self, *paths: Path) -> dict:
        """Use the artifact's packages; carry steps also select the target tree's source."""
        environment = {**self.runtime.environment(os.environ), "HOME": str(self.home)}
        if paths:
            imports = SpecifiedImportPath(os.pathsep.join(map(str, paths)))
            configuration: RestartEnvironment = replace(
                RestartEnvironment.inherit(environment), pythonpath=imports)
            return configuration.apply_runtime(environment)
        return environment

    def cli[Answer: CliResult](self, root: Path, command: type[CliCommand[Answer]],
                               *arguments: str) -> Answer:
        """This bus's answer to ``command``, never the route's, decoded as the result the
        command declares. A declared read-unavailable failure (the route index still
        rebuilding, its writer busy) is waited out; every other failure refuses."""
        invocation = command.invocation(*arguments)
        verb = f"agent-comms {' '.join(invocation)}"
        for _ in range(240):
            with self.commands.timing(verb):
                completed = subprocess.run(
                    [str(self.python), "-m", cli.__name__, "--root", str(root), *invocation],
                    cwd=self.checkout, env=self.environment(), capture_output=True, text=True)
            try:
                if not completed.returncode:
                    return FieldCodec.decode(command.result_type(),
                                             FieldCodec.parse(completed.stdout))
                failure = CORE_ERROR.validate_json(completed.stdout)
            except (MalformedRecord, ValueError) as malformed:
                raise UpgradeRefused(f"{verb}: undecodable answer: {malformed}; "
                                     f"stderr: {completed.stderr.strip()}") from malformed
            if not issubclass(failure.exception_type, CoordinationReadUnavailable):
                raise UpgradeRefused(f"{verb}: {failure.error}")
            time.sleep(5)
        raise UpgradeRefused(f"{verb}: still unavailable after 20 minutes: {failure.error}")

    def call_step[Answer](self, step: Carry, tree: TargetTree,
                          operation: CarryOperation[Answer]) -> Answer:
        """Run the step in the target tree with this stack's interpreter."""
        with tempfile.TemporaryDirectory() as scratch:
            result = Path(scratch) / "result.json"
            self.commands.run(self.python, "-m", step_worker.__name__, step.name(),
                              json.dumps(FieldCodec.encode(step)),
                              json.dumps(FieldCodec.encode(operation)), result, cwd=tree.path,
                              env=self.environment(*tree.source_paths()))
            return operation.decode_answer(result.read_text())

    def backup_files(self, pending: tuple[Carry, ...],
                     tree: TargetTree) -> tuple[Path, ...]:
        return tuple(sorted({
            path for step in pending
            for path in self.call_step(step, tree, BackupFilesOperation())
        }))

    def running_owners(self, root: Path) -> list[str]:
        """Use the restart request's receiving declaration in the selected runtime."""
        return self.probe(RunningOwnersProbe(root))


    def owners(self, root: Path, command: type[CliCommand[CliResult]],
               names: list[str]) -> None:
        """Run the captured owner set together and join every request before proceeding."""
        with ThreadPoolExecutor(max_workers=max(1, len(names))) as pool:
            list(pool.map(lambda name: self.cli(root, command, "--name", name), names))

    def start(self, root: Path, names: list[str]) -> tuple[OwnerStart, ...]:
        """Start the captured owner set together; each owner's outcome is its own."""
        with ThreadPoolExecutor(max_workers=max(1, len(names))) as pool:
            return tuple(pool.map(lambda name: OwnerStart.attempt(self, root, name), names))

    def verify(self, root: Path) -> BusReadBack:
        """What the carried bus answers through the CLI of the runtime that serves it next."""
        return self.cli(root, ReadBackCliCommand)


@dataclass(frozen=True)
class TargetTree:
    """The temporary worktree of the target revision the install runs from."""

    repo: Path
    scratch: Path
    revision: str

    @property
    def path(self) -> Path:
        return self.scratch / f"tree-{self.revision[:12]}"

    def source_paths(self) -> tuple[Path, Path]:
        """What the install imports from: the tree's ``src``, then its carry steps' package."""
        return self.path / "src", self.path / STEPS.parent

    def add(self) -> None:
        ScratchCheckout.add(self.repo, self.path, self.revision, "--detach", "-q")

    def close(self) -> None:
        """Retire the tree; a failed add left nothing to retire."""
        if self.path.is_dir():
            ScratchCheckout(self.path).retire()


@dataclass(frozen=True)
class PracticeBus:
    """The one practice copy of an install. The carry steps' dry runs and the rehearsal
    both run on it, so no process of the staged runtime opens the live bus before
    maintenance holds it: opening a store reconciles its schema, and on 2026-10-22
    install 53's dry runs left the live store on the target's schema, which the outgoing
    runtime's own stop then refused."""

    scratch: Path
    copied: Path
    measurement: PracticeMeasurement
    original_root: Path
    spill_dir: Path

    @classmethod
    @contextmanager
    def of(cls, root: Path, installer: InstallerChoice, spill_dir: Path
           ) -> Iterator[PracticeBus]:
        parent = installer.scratch(StorageRule.load(root))
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        spill_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (
            tempfile.TemporaryDirectory(dir=parent, prefix="practice-") as scratch,
            tempfile.TemporaryDirectory(dir=spill_dir, prefix="practice-carry-") as spill,
            practice_copy(root, Path(scratch) / "bus") as (copied, measurement),
        ):
            yield cls(Path(scratch), copied, measurement, root, Path(spill))

    def pending(self, chain: CarryChain, staged: Stack,
                tree: TargetTree) -> tuple[type[Carry], ...]:
        """Each step's dry run with the target's staged stack says what is left."""
        return chain.pending(lambda step: staged.call_step(
            step(self.copied, self.original_root, self.spill_dir), tree,
            MigrateOperation(apply=False)))


def upgrade(checkout: Path, root: Path, target: str, installer: InstallerChoice, rehearse_only: bool,
            retire: bool, spill_dir: Path, scratch: Path,
            harnesses: Callable[[], InstallResult], report: InstallReport) -> Upgrade:
    """``scratch`` holds the target's worktree the launcher runs this source from; this
    run owns it and removes it. A live run ends by registering the ``harnesses``."""
    commands = InstallCommands()
    current = commands.run("git", "-C", checkout, "rev-parse", "HEAD").strip()
    tree = TargetTree(checkout, scratch, target)
    try:
        ancestry = ["git", "-C", str(checkout), "merge-base", "--is-ancestor", current, target]
        with commands.timing(" ".join(ancestry)):
            answer = subprocess.run(ancestry)
        if answer.returncode:
            raise UpgradeRefused(f"{target[:12]} does not follow the checkout's {current[:12]}")
        if commands.run("git", "-C", checkout, "status", "--porcelain",
                        "--untracked-files=no").strip():
            raise UpgradeRefused("The checkout has local changes")
        try:
            chain = CarryChain.at(tree.path)
        except ValueError as error:
            raise UpgradeRefused(str(error)) from None
        member = Install.member(root, retire)
        installed = Stack(checkout, commands)
        installed.require_uv()
        if not installed.python.exists():
            raise UpgradeRefused(f"No installed stack at {installed.python}")
        staged = Stack.build(tree.path, commands)
        install, rehearsal = member.rehearsed(checkout, root, current, target, chain,
                                              installed, staged, tree, installer, spill_dir)
        names = tuple(step.name() for step in install.pending)
        if rehearse_only:
            return RehearsedUpgrade(current, target, root, names, rehearsal,
                                    commands=tuple(commands.ran))
        install.announce(report)
        bridges = SessionBridgeResumes()
        live_run = install.install(rehearsal, bridges)
        finished = InstalledUpgrade(current, target, root, names, rehearsal, live_run,
                                    harnesses(), commands=tuple(commands.ran))
        result = (finished.extended(UnresumedUpgrade, unresumed=bridges.lines())
                  if bridges.failures else finished)
        report.finished(result)
        bridges.require_resumed()
        return result
    except UpgradeRefused as error:
        if not rehearse_only:
            report.failed(error)
        raise
    finally:
        tree.close()
        shutil.rmtree(scratch, ignore_errors=True)


@dataclass(frozen=True)
class Install[Rehearsed: Rehearsal](ABC):
    """One run once the target's stack is relocked: what it changes on the bus. Its live
    pass takes the rehearsal its own kind produced."""

    checkout: Path
    root: Path
    current: str
    target: str
    pending: tuple[Carry, ...]
    installed: Stack
    staged: Stack
    tree: TargetTree
    installer: InstallerChoice
    spill_dir: Path

    def announce(self, report: InstallReport) -> None:
        report.starting(Upgrade(self.current, self.target, self.root,
                                tuple(step.name() for step in self.pending)))

    @staticmethod
    def member(root: Path, retire: bool) -> type[Install]:
        """What exists at ``root`` decides how much of an install it needs."""
        if retire:
            return RetiringInstall
        if BusFile.fresh(root):
            return FreshInstall
        return ExistingBusInstall

    @classmethod
    @abstractmethod
    def rehearsed(cls, checkout: Path, root: Path, current: str, target: str,
                  chain: CarryChain, installed: Stack, staged: Stack, tree: TargetTree,
                  installer: InstallerChoice, spill_dir: Path) -> tuple[Install, Rehearsal]:
        """This member's install, with what is pending, and its rehearsal."""

    def install(self, rehearsal: Rehearsed, bridges: SessionBridgeResumes) -> LiveRun:
        """Session bridges close their bus resources before units stop, then exec in place.

        Each serve retains its recorded owner's PID and start time after the hook exits.
        Its control connection survives until live() publishes or rolls back; the
        installed pointer, not a copied target version, selects its successor.
        """
        with ExitStack() as resources:
            for bridge in Serve.running(self.installed.home):
                bridges.pause(bridge, resources)
            resources.callback(bridges.resume)
            return self.live(rehearsal)

    @abstractmethod
    def live(self, rehearsal: Rehearsed) -> LiveRun: ...


class ExistingBusInstall[Rehearsed: Rehearsal](Install[Rehearsed]):
    """A bus is already at the root. When a carry step exists or a store's schema differs
    from the target's, one practice copy of it answers what is pending and carries the
    rehearsal; otherwise nothing could change, and no copy is taken."""

    @classmethod
    def rehearsed(cls, checkout: Path, root: Path, current: str, target: str,
                  chain: CarryChain, installed: Stack, staged: Stack, tree: TargetTree,
                  installer: InstallerChoice, spill_dir: Path) -> tuple[Install, Rehearsal]:
        if chain.steps or staged.cli(root, PlannedReconcileCliCommand).rewritten:
            with PracticeBus.of(root, installer, spill_dir) as practice:
                install = cls.of(checkout, root, current, target,
                                 practice.pending(chain, staged, tree),
                                 installed, staged, tree, installer, spill_dir)
                return install, install.rehearsal(practice)
        install = cls.of(checkout, root, current, target, (), installed, staged, tree,
                         installer, spill_dir)
        return install, install.uncopied()

    @classmethod
    def of(cls, checkout: Path, root: Path, current: str, target: str,
           pending: tuple[type[Carry], ...], installed: Stack, staged: Stack, tree: TargetTree,
           installer: InstallerChoice, spill_dir: Path) -> ExistingBusInstall:
        """A bus already carried, on the target, built from the installed lock needs nothing."""
        unchanged = not pending and current == target and RuntimeResolution.installed().matches(
            staged.lock, staged.python, target, staged.commands)
        member = (ChangedInstall, UnchangedInstall)[unchanged]
        steps = tuple(step(root, root, spill_dir) for step in pending)
        return member(checkout, root, current, target, steps, installed, staged,
                      tree, installer, spill_dir)

    @abstractmethod
    def rehearsal(self, practice: PracticeBus) -> Rehearsed: ...

    @abstractmethod
    def uncopied(self) -> Rehearsed:
        """This member's rehearsal when nothing on the bus could change."""


@dataclass(frozen=True)
class PreparedMigration(Extendable):
    """The held bus as the outgoing runtime read it, before any migration."""

    hold: MaintenanceState
    before: dict[str, SavedFile]
    read: BusReadBack

    @property
    def root(self) -> Path:
        return Path(self.hold.root)

    @classmethod
    @contextmanager
    def capture(cls, hold: MaintenanceState, outgoing: Stack) -> Iterator[PreparedMigration]:
        """Keep the snapshot stable until its caller has finished taking any backup."""
        root = Path(hold.root)
        with transcripts_held(root):
            before = bus_files(root)
            yield cls(hold, before, outgoing.verify(root))

    def carry(self, pending: tuple[Carry, ...], staged: Stack,
              tree: TargetTree) -> CarriedPass:
        """One migration sequence for rehearsal and the backed-up live bus."""
        steps = []
        for step in pending:
            steps.append(StepRun(step.name(), staged.call_step(
                step, tree, MigrateOperation(apply=True))))
        reconciled = staged.cli(self.root, ReconcileStoresCliCommand)
        steps.append(StepRun(ReconcileStoresCliCommand.declared_name, Carried(
            len(reconciled.rewritten), rewritten=reconciled.rewritten)))
        declared = tuple(steps)
        files = carried(self.root, self.before, declared, RehearsalFiles.declared(declared))
        passed = CarriedPass(declared, files, staged.verify(self.root))
        passed.require_read(self.read)
        return passed


@dataclass(frozen=True)
class BackedUpMigration(PreparedMigration):
    """A live migration carries the recovery data its failure path needs."""

    backup: ResourceBackup

    def restore(self) -> None:
        if not self.backup.original():
            self.backup.restore()


class ChangedInstall(ExistingBusInstall[RehearsedFiles]):
    def rehearsal(self, practice: PracticeBus) -> CarriedRehearsal:
        copied = practice.copied
        home = practice.scratch / "home"
        RehearsalHome(home).prepare()
        (home / ".claude").symlink_to(Path.home() / ".claude")
        stack = replace(self.staged, home=home)
        stack.prepare_native()
        pending = tuple(replace(step, root=copied, spill_dir=practice.spill_dir)
                        for step in self.pending)
        for destination in stack.backup_files(pending, self.tree):
            source = self.root / destination.relative_to(copied)
            if source.exists() and not destination.exists():
                destination.parent.mkdir(parents=True, exist_ok=True)
                RootFile.content_owner(source).copy_file(source, destination)
        self.installed.running_owners(copied)
        maintenance = MaintenanceBarrier(RegistryStore.at(copied).path)
        hold = maintenance.begin(MaintenanceOperator.current())
        with hold.holding():
            with PreparedMigration.capture(hold, self.installed) as migration:
                pass
            passed = migration.carry(pending, stack, self.tree)
            passed = passed.after_acceptance(pending, stack, self.tree)
        maintenance.clear(hold)
        return passed.extended(CarriedRehearsal, copy=practice.measurement)

    def uncopied(self) -> UncopiedRehearsal:
        return UncopiedRehearsal()

    def live(self, rehearsal: RehearsedFiles) -> CarriedRun:
        expected = frozenset(rehearsal.files.names) | frozenset(
            str(path.relative_to(self.root))
            for path in self.staged.backup_files(self.pending, self.tree))
        selected = tuple(self.root / name for name in sorted(expected))
        restore = RestoreSpace.measure(max(
            rehearsal.files.restore_bytes,
            sum(path.stat().st_size for path in selected if path.exists()),
        ), self.root.parent)
        restore.require()
        running = self.installed.running_owners(self.root)
        maintenance = MaintenanceBarrier(RegistryStore.at(self.root).path)
        held = maintenance.begin(MaintenanceOperator.current())
        holder = ExitStack()
        holder.enter_context(held.holding())  # this run's own CLI calls read under its hold
        try:
            try:
                self.installed.owners(self.root, StopCliCommand, running)
                planned = self.staged.cli(self.root, PlannedReconcileCliCommand)
                unrehearsed = sorted(frozenset(planned.rewritten) - frozenset(rehearsal.files.names))
                if unrehearsed:
                    raise UpgradeRefused(
                        f"Reconcile would rewrite files the rehearsal did not: {unrehearsed}")
                with PreparedMigration.capture(held, self.installed) as original:
                    migration = original.extended(BackedUpMigration, backup=StoreBackup.take(
                        self.root, active_route_path(), selected, self.spill_dir))
            except BaseException as error:
                holder.close()
                maintenance.clear(held)
                started = self.installed.start(self.root, running)
                print(f"install: aborted before the backup ({type(error).__name__}: {error}); "
                      f"maintenance cleared; owners: {started}", file=sys.stderr)
                raise
            try:
                self.staged.commands.run("git", "-C", self.checkout, "merge", "--ff-only", "-q", self.target)
                Checkout(self.checkout).prepare()
                self.staged.prepare_native()
                passed = migration.carry(self.pending, self.staged, self.tree)
                passed.files.require_rehearsal(rehearsal.files)
                self.staged.publish()
                holder.close()
                maintenance.clear(held)
                services = HostService.restart_running(self.staged.python)
            except BaseException as error:
                print(f"install: install failed ({type(error).__name__}: {error}); "
                      "restoring original installation", file=sys.stderr)
                state = maintenance.read()
                recovery = state.phase.hold_for(maintenance, state, MaintenanceOperator.current())
                holder.close()
                holder.enter_context(recovery.holding())
                migration.restore()
                restored = maintenance.read()
                restored_hold = restored.phase.hold_for(
                    maintenance, restored, MaintenanceOperator.current())
                holder.close()
                holder.enter_context(restored_hold.holding())
                head = self.installed.commands.run("git", "-C", self.checkout, "rev-parse", "HEAD").strip()
                if head != self.current:
                    self.installed.commands.run("git", "-C", self.checkout, "reset", "-q", "--keep", self.current)
                    Checkout(self.checkout).prepare()
                self.installed.publish()
                holder.close()
                maintenance.clear(restored_hold)
                started = self.installed.start(self.root, running)
                print(f"install: restored after failure ({type(error).__name__}: {error}); "
                      f"original runtime selected; owners: {started}; "
                      f"backup: {migration.backup.description}",
                      file=sys.stderr)
                raise InstallRolledBack(error, started, migration.backup.description,
                                        runtime=self.installed.runtime) from error
            # Installed: the carries ran and the runtime is published. Starting the owners it
            # stopped is reported per owner and never rolls the install back.
            try:
                retirement = tuple(replace(
                    step, spill_dir=migration.backup.spill_directory(self.spill_dir),
                ) for step in self.pending)
                passed = passed.after_acceptance(retirement, self.staged, self.tree)
            finally:
                started = self.staged.start(self.root, running)
            retired_backups = migration.backup.retain(self.spill_dir)
            return passed.extended(CarriedRun, stopped=tuple(running), services=services,
                                   started=started, restore_space=restore, backup=migration.backup.report(),
                                   retired_backups=retired_backups)
        finally:
            holder.close()


class UnchangedInstall(ExistingBusInstall[UnchangedRehearsal]):
    """The checkout already sits at the target and the relocked lock is the installed one:
    nothing to rehearse, hold or restart."""

    def rehearsal(self, practice: PracticeBus) -> UnchangedRehearsal:
        return UnchangedRehearsal()

    def uncopied(self) -> UnchangedRehearsal:
        return UnchangedRehearsal()

    def live(self, rehearsal: UnchangedRehearsal) -> UnchangedRun:
        self.installed.publish()
        return UnchangedRun()


class FreshInstall(Install[FreshRehearsal]):
    """Nothing was ever written at the root: no carry, rehearsal, hold or owner to restart.
    The target's source creates the bus already in its format and routes to it."""

    @classmethod
    def rehearsed(cls, checkout: Path, root: Path, current: str, target: str,
                  chain: CarryChain, installed: Stack, staged: Stack, tree: TargetTree,
                  installer: InstallerChoice, spill_dir: Path) -> tuple[Install, Rehearsal]:
        """Nothing at the root to copy, so nothing is pending."""
        install = cls(checkout, root, current, target, (), installed, staged,
                      tree, installer, spill_dir)
        return install, install.rehearsal()

    def announce(self, report: InstallReport) -> None:
        """A fresh bus has nowhere to publish a start message yet."""

    def rehearsal(self) -> FreshRehearsal:
        return FreshRehearsal()

    def live(self, rehearsal: FreshRehearsal) -> CreatedRun:
        self.staged.commands.run("git", "-C", self.checkout, "merge", "--ff-only", "-q",
                                 self.target)
        Checkout(self.checkout).prepare()
        self.staged.prepare_native()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        Comms(self.root).registry.prepare_native_root()
        bus = Bus.install(Bus.path_in(self.root))
        self.staged.publish()
        route = route_new_bus(ActiveRoute(
            self.root.absolute(), RecordedBusIdentity.capture(bus.path)), active_route_path())
        services = HostService.restart_running(self.staged.python)
        return CreatedRun(services, route)


class RetiringInstall(FreshInstall):
    """The bus at the root is runtime state the target cannot carry: its owners stop, it
    moves aside intact, and a fresh bus takes its place."""

    def rehearsal(self) -> RetiringRehearsal:
        return RetiringRehearsal()

    def live(self, rehearsal: FreshRehearsal) -> RetiredRun:
        running = self.installed.running_owners(self.root)
        self.installed.owners(self.root, StopCliCommand, running)
        retired = self.root.with_name(f"{self.root.name}.retired-{time.strftime('%Y%m%dT%H%M%S')}")
        self.root.rename(retired)
        return super().live(rehearsal).extended(RetiredRun, stopped=tuple(running), retired=retired)
