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
import sys
import tempfile
import time
import tomllib
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, ExitStack, contextmanager
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, ClassVar, Self

from pydantic import TypeAdapter

from agent_comms.active_route import ActiveRoute, RouteFile, active_route_path, route_directory
from agent_comms.bus import Bus
from agent_comms.cli_bridges.shared import BridgeResume, Serve, SessionBridgeResumes
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
from agent_comms.comms import wire
from agent_comms.coordination_errors import CoordinationReadUnavailable
from agent_comms.declared_family import DeclaredFamily
from agent_comms.field_codec import FieldCodec, MalformedRecord, PathText
from agent_comms.host_services import HostService, RestartedService
from agent_comms.install_choices import InstallerChoice, RunMode
from agent_comms.installer import publication, step_worker
from agent_comms.installer.backup import (
    BackupReport,
    RestoreSpace,
    SavedFile,
    StoreBackup,
    UpgradeRefused,
    bus_files,
)
from agent_comms.installer.carry import (
    STEPS,
    Carried,
    Carry,
    CarryChain,
    CarryOperation,
    CarryRequest,
    FirstPendingOperation,
    MigrateOperation,
    RetireInputsOperation,
)
from agent_comms.installer.commands import InstallCommands, TimedCommand
from agent_comms.installer.interpreter_operation import (
    CliCommandProbe,
    ClientImportsProbe,
    InstalledProgram,
    PackageRootProbe,
    PrepareNativeProbe,
    ProbeProgram,
    RetainedProgram,
    RouteSelection,
    RunningOwnersProbe,
    RuntimeProbe,
    SelectedRouteProbe,
    ServingProbe,
    SourceProgram,
)
from agent_comms.installer.practice import (
    PracticeCopy,
    PracticeMeasurement,
    RehearsalHome,
    RuntimeHome,
)
from agent_comms.installer.reporting import (
    RootWriter, RuntimeWriter, UnpostedWriter,
)
from agent_comms.installer.runtime_resolution import RuntimeResolution, StackProject
from agent_comms.launch_runtime import LaunchRuntime, RestartEnvironment, SpecifiedImportPath
from agent_comms.locked_store import BusFile, DeclaredFile, RootFile, UndeclaredFile
from agent_comms.native_build_inputs import NativeBuildInputs
from agent_comms.native_package import pinned_package_for
from agent_comms.root_runtime import RootRuntime, RootRuntimeStore
from agent_comms.runtime_processes import RuntimeProcesses
from agent_comms.maintenance_barrier import (
    MaintenanceBarrier,
    MaintenanceOperator,
    MaintenanceState,
)
from agent_comms.recorded_bus import RecordedBusIdentity
from agent_comms.registry_store import RegistryStore
from agent_comms.repo import Checkout, Repo, ScratchCheckout
from agent_comms.storage_rules import StorageRule

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
    names: tuple[Annotated[Path, PathText], ...]
    restore_bytes: int

    @classmethod
    def none(cls) -> RehearsalFiles:
        """The files of a rehearsal that rewrote nothing."""
        return cls(0, 0, (), 0)

    @staticmethod
    def declared(steps: tuple[StepRun, ...]) -> frozenset[Path]:
        return frozenset(name for step in steps for name in step.carried.rewritten)

    def require_rehearsal(self, rehearsal: RehearsalFiles) -> None:
        outside = sorted(set(self.names) - set(rehearsal.names))
        if outside:
            raise UpgradeRefused(f"Live carries changed files absent from rehearsal: {outside}")



def carried(
    root: Path,
    before: dict[DeclaredFile | UndeclaredFile, dict[Path, SavedFile]],
    steps: tuple[StepRun, ...], expected: frozenset[Path],
) -> RehearsalFiles:
    """Only the files a step names as rewritten may differ from ``before``."""
    after = bus_files(root)
    physical = {key for key in before.keys() | after.keys()
                if before.get(key) != after.get(key)}
    members = {member: resource.path for resource, snapshot in (*before.items(), *after.items())
               for member in snapshot}
    changed = {resource.path for resource in physical}
    declared = {members.get(path, path) for path in RehearsalFiles.declared(steps)}
    unnamed = sorted(changed - declared)
    if unnamed:
        raise UpgradeRefused(f"Carries changed files no step names: {unnamed}")
    outside = sorted(changed - {members.get(path, path) for path in expected})
    if outside:
        raise UpgradeRefused(f"Carries changed files outside the expected set: {outside}")
    return RehearsalFiles(
        identical=len({resource.path for resource in before} - changed), rewritten=len(changed),
        names=tuple(sorted(changed)),
        restore_bytes=sum(file.size for key in physical & before.keys()
                          for file in before[key].values()))


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


@dataclass(frozen=True)
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
            StepRun(step.declared_name, retired) for step, retired in
            zip(pending, stack.call_steps(pending, tree, RetireInputsOperation()), strict=True)
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


@dataclass(frozen=True)
class RehearsedFiles(Rehearsal, declared_name=None):
    """A rehearsal that names the files its live pass may rewrite."""

    files: RehearsalFiles
    inputs: tuple[Annotated[Path, PathText], ...]


@dataclass(frozen=True)
class CarriedRehearsal(CarriedPass, RehearsedFiles):
    copy: PracticeMeasurement


@dataclass(frozen=True)
class UncopiedRehearsal(RehearsedFiles):
    """A read of the live root found no carry pending and no store to reconcile, so no
    practice copy was taken and the live pass may rewrite nothing. Its own planned
    reconcile, under the hold, still refuses before any write if it names a file."""

    files: RehearsalFiles = field(default_factory=RehearsalFiles.none)
    inputs: tuple[Annotated[Path, PathText], ...] = ()


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

    finished: ClassVar[str] = "Install rehearsed"

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
    processes: RuntimeProcesses = field(kw_only=True, default=RuntimeProcesses(()))
    """Applications still running an outgoing runtime; reported, never signalled."""
    bridge_resumes: tuple[BridgeResume, ...] = field(kw_only=True, default=())

    finished: ClassVar[str] = "Install finished"
    """The headline of the post that reports this upgrade."""

    def lines(self) -> tuple[str, ...]:
        return (*super().lines(), *self.live.lines(), *self.processes.lines(),
                *(line for outcome in self.bridge_resumes for line in outcome.lines()))


@dataclass(frozen=True)
class UnresumedUpgrade(InstalledUpgrade):
    """Installed, with session bridges paused for the swap that did not come back: the
    install exits non-zero after reporting it."""

    finished: ClassVar[str] = "Install finished; session bridges not resumed"


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

    def resolve(self) -> None:
        """Refresh declared source packages while retaining the committed release pins."""
        with self.lock.with_name("pyproject.toml").open("rb") as definition:
            project = StackProject.model_validate(tomllib.load(definition))
        upgrades = tuple(argument for name in project.tool.uv.sources
                         for argument in ("--upgrade-package", name))
        self.commands.run(self.uv, "lock", "--project", "stack", *upgrades,
                          "--python", "3.14", cwd=self.checkout)

    @classmethod
    def build(cls, checkout: Path, commands: InstallCommands, home: Path = HOME) -> Stack:
        """Resolve current mains once, then build or reuse their immutable installation."""
        Checkout(checkout).prepare()
        source = cls(checkout, commands, home, runtime=LaunchRuntime.running())
        source.require_uv()
        source.resolve()
        core = commands.run("git", "-C", checkout, "rev-parse", "HEAD").strip()
        resolution = hashlib.sha256(source.lock.read_bytes()).hexdigest()[:16]
        prefix = home / ".local/share/agent-comms/runtimes" / f"core-{core[:9]}-lock-{resolution}"
        built = replace(source, runtime=LaunchRuntime(prefix / "bin/python"))
        if prefix.exists():
            if not RuntimeResolution.at(prefix).matches(source.lock, built, core):
                raise UpgradeRefused("Existing built runtime differs from its resolved inputs")
            built.stage_native()
            built.prepare_native()
            return built
        prefix.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        prefix.mkdir(mode=0o700)
        try:
            commands.run(source.uv, "sync", "--project", "stack", "--locked", "--python", "3.14",
                         "--no-editable", cwd=checkout,
                         env={**built.environment(), "UV_PROJECT_ENVIRONMENT": str(prefix)})
            built.install_native()
            RuntimeResolution.capture(built, core).save(prefix)
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
                "timeout", "300", self.python, "-m", "pytest", "-q", "-s",
                "-o", "pythonpath=. tools", "-o", "faulthandler_timeout=60",
                "--basetemp", Path(temporary) / "pytest", "-p", "no:cacheprovider",
                *pytest_arguments, cwd=self.checkout, env=self.environment()), end="")

    def probe[Answer](self, probe: RuntimeProbe[Answer], *,
                      source: tuple[Path, ...] = (),
                      program: ProbeProgram = SourceProgram()) -> Answer:
        return self.commands.probe(probe, self.python, cwd=self.home,
                                   env=self.environment(*source), program=program)

    def imports(self) -> None:
        self.probe(ClientImportsProbe())

    @property
    def prepared_natives(self) -> Path:
        """Every runtime's built native package, laid out as ``stack/`` is and keyed by the
        pin, so a runtime whose pin was already built copies it instead of rebuilding it."""
        return self.home / ".local/share/agent-comms/native"

    def stage_native(self) -> tuple[Path, Path]:
        """The checkout's ``stack/`` holds this pin's build, because the checkout's own source
        acquires its pin there (the installer runs that source and validates the route it
        publishes with it). Copied from the prepared build, or built when no install has
        built this pin yet. Answers the staged build and its layout under ``stack/``."""
        manifest = self.checkout / "stack/pi-native.sha256"
        try:
            NativeBuildInputs(self.checkout).require_pinned(manifest)
        except ValueError as error:
            raise UpgradeRefused(str(error)) from None
        native = pinned_package_for(manifest)
        layout = native.relative_to(manifest.parent)
        prepared = self.prepared_natives / layout
        if native.is_dir():
            return native, layout
        if prepared.is_dir():
            native.parents[2].mkdir(mode=0o700)
            shutil.copytree(prepared, native)
        else:
            self.commands.run(self.checkout / "stack/bin/prepare-pi-native", "--python",
                              self.python, cwd=self.checkout,
                              env=self.environment(self.checkout / "src"))
            self.keep_native(native, layout)
        return native, layout

    def install_native(self) -> None:
        """Copy this pin's staged native package into the interpreter; qualifying the copy
        checks it against the pin."""
        native, layout = self.stage_native()
        package = self.probe(PackageRootProbe())
        destination = package / "_native" / layout
        destination.parents[2].mkdir(mode=0o700, parents=True)
        shutil.copytree(native, destination)
        self.prepare_native()

    def keep_native(self, native: Path, layout: Path) -> None:
        """Publish a fresh build under its pin whole, and retire other pins' builds: each
        runtime holds its own copy, so only the next runtime reads this one."""
        self.prepared_natives.mkdir(mode=0o700, parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(dir=self.prepared_natives, prefix=".stage-"))
        try:
            shutil.copytree(native, stage / layout)
            for retired in self.prepared_natives.glob(".pi-native-*"):
                shutil.rmtree(retired)
            stage.joinpath(layout.parts[0]).rename(self.prepared_natives / layout.parts[0])
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    def prepare_native(self) -> None:
        self.probe(PrepareNativeProbe())

    def publish(self, root: Path) -> None:
        """The selected interpreter owns console metadata and harness declarations, and
        ``root`` belongs to this stack's runtime from now on."""
        self.commands.run(
                          self.python, "-m", publication.__name__,
                          cwd=self.checkout, env=self.environment(),
        )
        RootRuntime.publish(root, self.runtime)

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
            completed = self.probe(CliCommandProbe(("--root", str(root), *invocation)))
            try:
                if not completed.returncode:
                    return FieldCodec.decode(command.result_type(),
                                             FieldCodec.parse(completed.stdout))
                failure = CORE_ERROR.validate_json(completed.stdout)
            except (MalformedRecord, ValueError) as malformed:
                raise UpgradeRefused(f"{verb}: undecodable answer: {malformed}; "
                                     f"stdout: {completed.stdout.strip()}") from malformed
            if not issubclass(failure.exception_type, CoordinationReadUnavailable):
                raise UpgradeRefused(f"{verb}: {failure.error}")
            time.sleep(5)
        raise UpgradeRefused(f"{verb}: still unavailable after 20 minutes: {failure.error}")

    def call_steps[Answer](self, steps: tuple[Carry, ...], tree: TargetTree,
                           operation: CarryOperation[Answer]) -> tuple[Answer, ...]:
        """Run one pass of steps, in order, in one worker in the target tree with this
        stack's interpreter; the operation owns traversal and its answer tuple."""
        if not steps:
            return ()
        request = CarryRequest(operation, steps)
        with self.commands.exchange(FieldCodec.compact(FieldCodec.encode(request))) as (request_path, result):
            self.commands.run(self.python, "-m", step_worker.__name__,
                              request_path, result,
                              cwd=tree.path, env=self.environment(*tree.source_paths()))
            return operation.decode_answer(result.read_text())

    def running_owners(self, root: Path) -> list[str]:
        """Use the restart request's receiving declaration in the selected runtime."""
        return self.probe(RunningOwnersProbe(root))

    def owners(self, root: Path, command: type[CliCommand[CliResult]],
               names: list[str]) -> None:
        """Run the captured owner set together and join every request before proceeding.

        Every request is submitted and the pool's exit joins them all; only then does a
        failure propagate, naming each failed owner. ``Executor.map`` is not used: it
        cancels the requests not yet started the moment the first result raises."""
        with ThreadPoolExecutor(max_workers=max(1, len(names))) as pool:
            requests = [pool.submit(self.cli, root, command, "--name", name) for name in names]
        if failures := [request.exception() for request in requests if request.exception()]:
            raise UpgradeRefused("; ".join(str(failure.args[0]) for failure in failures)
                                 ) from failures[0]

    def start(self, root: Path, names: list[str]) -> tuple[OwnerStart, ...]:
        """Start the captured owner set together; each owner's outcome is its own."""
        with ThreadPoolExecutor(max_workers=max(1, len(names))) as pool:
            return tuple(pool.map(lambda name: OwnerStart.attempt(self, root, name), names))

    def restart_services(self) -> tuple[RestartedService, ...]:
        """Every running host service moves onto this runtime, which owns the root and so
        proves each one serves it."""
        services = HostService.restart_running(self.python)
        self.probe(ServingProbe())
        return services

    def start_services(self, stopped: tuple[HostService, ...]) -> tuple[RestartedService, ...]:
        services = tuple(service.start_onto(self.python) for service in stopped)
        self.probe(ServingProbe())
        return services

    def verify(self, root: Path) -> BusReadBack:
        """What the carried bus answers through the CLI of the runtime that serves it next."""
        return self.cli(root, ReadBackCliCommand)


@dataclass(frozen=True)
class TargetTree(AbstractContextManager):
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

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exception: object) -> None:
        self.close()
        if self.scratch.exists():
            shutil.rmtree(self.scratch)


@dataclass(frozen=True)
class PracticeBus:
    """Pending selection dry-runs every step on the live root before maintenance holds
    it, so nothing is copied to choose: a dry run reads the live stores under their own
    locks and writes no store data, because every carry writes only when applying.
    Taking a store's lock creates its lock file, and that file's directory, when it
    does not exist yet; live writers wait while a dry run holds a lock. Reading a WAL
    database updates its shared-memory index (``-shm``), as every SQLite reader does,
    and creates the index when it is missing. Apply runs use the selected practice
    inputs, then the backed-up live root under maintenance."""

    scratch: Path
    copy: PracticeCopy
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
        ):
            yield cls(Path(scratch), PracticeCopy(root, Path(scratch) / "bus"), root, Path(spill))

    def pending(self, chain: CarryChain, staged: Stack,
                tree: TargetTree) -> tuple[type[Carry], ...]:
        """Read-only dry runs in one worker stop at the first step that says work remains."""
        if not chain.steps:
            return ()
        steps = tuple(step(self.original_root, self.original_root, self.spill_dir)
                      for step in chain.steps)
        (position,) = staged.call_steps(steps, tree, FirstPendingOperation())
        return chain.steps[position:]

    def select(self, pending: tuple[Carry, ...], reconciled: tuple[Path, ...]) -> tuple[Path, ...]:
        # This is the one input union consumed by both rehearsal and live backup.
        paths = tuple(sorted({
            RegistryStore.at(self.original_root).path,
            RootRuntimeStore.path_in(self.original_root),
            *(self.original_root / path for path in reconciled),
            *(path for step in pending for path in step.input_files()),
        }))
        self.copy.include(paths)
        self.copy.retain(paths)
        return tuple(sorted(path.relative_to(self.original_root) for path in self.copy.copied))


def upgrade(checkout: Path, root: Path, target: str, installer: InstallerChoice, mode: RunMode,
            retire: bool, spill_dir: Path, scratch: Path,
            harnesses: Callable[[Stack], InstallResult], report: InstallReport) -> Upgrade:
    """``scratch`` holds the target's worktree the launcher runs this source from; this
    run owns it and removes it. A live run ends by registering the ``harnesses``."""
    commands = InstallCommands()
    tree = TargetTree(checkout, scratch, target)
    bridges = report.bridges
    with commands, report.running(tree):
        StoreBackup.complete_interrupted(root, spill_dir)
        installed = Stack(tree.path, commands)
        current = commands.run("git", "-C", checkout, "rev-parse", "HEAD").strip()
        with commands.timing(f"git merge-base --is-ancestor {current} {target}"):
            follows = Repo.descends(checkout, current, target)
        if not follows:
            raise UpgradeRefused(f"{target[:12]} does not follow the checkout's {current[:12]}")
        if commands.run("git", "-C", checkout, "status", "--porcelain",
                        "--untracked-files=no").strip():
            raise UpgradeRefused("The checkout has local changes")
        try:
            chain = CarryChain.at(tree.path)
        except ValueError as error:
            raise UpgradeRefused(str(error)) from None
        member = Install.member(root, retire)
        installed.require_uv()
        if not installed.python.exists():
            raise UpgradeRefused(f"No installed stack at {installed.python}")
        staged = Stack.build(tree.path, commands)
        report.starting(mode.destination(member.initial_writer(root, installed)), target)
        install, rehearsal = member.rehearsed(checkout, root, current, target, chain,
                                              installed, staged, tree, installer, spill_dir)
        result = report.complete(mode.finish(install, rehearsal, bridges, harnesses, report))
    bridges.require_resumed()
    return result


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

    @staticmethod
    def initial_writer(root: Path, installed: Stack) -> RootWriter:
        """The outgoing runtime posts until the target's runtime takes the bus over, unless
        the route file refuses it as a borrower; install's own route step replaces that file."""
        return RouteFile.read(active_route_path()).outgoing_writer(
            RuntimeWriter(root, installed, RetainedProgram.current()), UnpostedWriter())

    def backed_up(self, files: tuple[Path, ...]) -> tuple[StoreBackup, RouteSelection]:
        backup = StoreBackup.take(self.root, active_route_path(), files, self.spill_dir)
        with route_directory(backup.route):
            backup.require_route_revision()
            selected = self.installed.probe(SelectedRouteProbe())
        return backup, selected

    def publish(self, backup: StoreBackup, selected: RouteSelection) -> str:
        """The converted bus's route is ready before new runtime entrypoints open it."""
        incoming = ActiveRoute(self.root.absolute(), RecordedBusIdentity.capture(Bus.path_in(self.root)))
        route = backup.rebind_route(incoming, selected)
        self.staged.publish(self.root)
        return route

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

    def reported(self, rehearsal: Rehearsed) -> RehearsedUpgrade:
        return RehearsedUpgrade(
            self.current, self.target, self.root,
            tuple(step.declared_name for step in self.pending), rehearsal,
            commands=tuple(self.staged.commands.ran))

    def finished(self, rehearsal: Rehearsed, bridges: SessionBridgeResumes,
                 harnesses: Callable[[Stack], InstallResult], report: InstallReport) -> InstalledUpgrade:
        rehearsed = self.reported(rehearsal)
        live_run = self.install(rehearsal, bridges, report)
        if self.installed.python != self.staged.python:
            self.staged.commands.close_runtime(self.installed.python)
        finished = InstalledUpgrade(
            rehearsed.source, rehearsed.target, rehearsed.root, rehearsed.pending,
            rehearsed.rehearsal, live_run, harnesses(self.staged), commands=tuple(self.staged.commands.ran),
            processes=RuntimeProcesses.observe(self.staged.runtime),
            bridge_resumes=tuple(bridges.outcomes))
        return (finished.extended(UnresumedUpgrade)
                if bridges.failures else finished)

    def install(self, rehearsal: Rehearsed, bridges: SessionBridgeResumes,
                report: InstallReport) -> LiveRun:
        """Session bridges close their bus resources before units stop, then exec in place.

        Each serve retains its recorded owner's PID and start time after the hook exits.
        Its control connection survives until live() publishes or rolls back; the
        installed pointer, not a copied target version, selects its successor.
        """
        with route_directory(active_route_path()):
            pass  # A live route reader refuses the install here, before any owner stops.
        with ExitStack() as resources:
            try:
                for bridge in Serve.running(self.installed.home):
                    bridges.pause(bridge, resources)
                return self.live(rehearsal, report)
            finally:
                # A later pause may fail; resume captured peers before closing their sockets.
                bridges.resume()

    @abstractmethod
    def live(self, rehearsal: Rehearsed, report: InstallReport) -> LiveRun: ...


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
            staged.lock, staged, target)
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
    before: dict[DeclaredFile | UndeclaredFile, dict[Path, SavedFile]]
    read: BusReadBack

    @property
    def root(self) -> Path:
        return Path(self.hold.root)

    @classmethod
    def capture(cls, hold: MaintenanceState, outgoing: Stack) -> PreparedMigration:
        """The maintenance barrier keeps every carried writer out until the caller's backup ends."""
        root = Path(hold.root)
        return cls(hold, bus_files(root), outgoing.verify(root))

    def carry(self, pending: tuple[Carry, ...], staged: Stack,
              tree: TargetTree) -> CarriedPass:
        """One migration sequence for rehearsal and the backed-up live bus."""
        steps = [StepRun(step.declared_name, carried) for step, carried in zip(
            pending, staged.call_steps(pending, tree, MigrateOperation(apply=True)), strict=True)]
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

    backup: StoreBackup

    def restore(self) -> None:
        if not self.backup.original():
            self.backup.restore()


class ChangedInstall(ExistingBusInstall[RehearsedFiles]):
    def rehearsal(self, practice: PracticeBus) -> CarriedRehearsal:
        copied = practice.copy.path
        home = practice.scratch / "home"
        RehearsalHome(home).prepare()
        (home / ".claude").symlink_to(Path.home() / ".claude")
        stack = replace(self.staged, home=home)
        pending = tuple(replace(step, root=copied, spill_dir=practice.spill_dir)
                        for step in self.pending)
        planned = self.staged.cli(self.root, PlannedReconcileCliCommand)
        inputs = practice.select(self.pending, planned.rewritten)
        self.installed.running_owners(copied)
        maintenance = MaintenanceBarrier(RegistryStore.at(copied).path)
        hold = maintenance.retarget(maintenance.begin(MaintenanceOperator.current()), stack.runtime)
        with hold.holding():
            migration = PreparedMigration.capture(hold, self.installed)
            passed = migration.carry(pending, stack, self.tree)
            passed = passed.after_acceptance(pending, stack, self.tree)
        maintenance.clear(hold)
        return passed.extended(CarriedRehearsal, copy=practice.copy.measurement, inputs=inputs)

    def uncopied(self) -> UncopiedRehearsal:
        return UncopiedRehearsal()

    def live(self, rehearsal: RehearsedFiles, report: InstallReport) -> CarriedRun:
        expected = frozenset(rehearsal.files.names) | frozenset(rehearsal.inputs)
        selected = tuple(self.root / name for name in sorted(expected))
        restore = RestoreSpace.measure(max(
            rehearsal.files.restore_bytes,
            sum(path.stat().st_size for path in selected if path.exists()),
        ), self.root.parent)
        restore.require()
        RootRuntimeStore.at(self.root).adopt(self.installed.runtime)
        running = self.installed.running_owners(self.root)
        maintenance = MaintenanceBarrier(RegistryStore.at(self.root).path)
        held = maintenance.begin(MaintenanceOperator.current())
        holder = ExitStack()
        holder.enter_context(held.holding())  # this run's own CLI calls read under its hold
        try:
            try:
                self.installed.owners(self.root, StopCliCommand, running)
                held = maintenance.retarget(held, self.staged.runtime)
                holder.close()
                holder.enter_context(held.holding())
                planned = self.staged.cli(self.root, PlannedReconcileCliCommand)
                unrehearsed = sorted(frozenset(planned.rewritten) - frozenset(rehearsal.files.names))
                if unrehearsed:
                    raise UpgradeRefused(
                        f"Reconcile would rewrite files the rehearsal did not: {unrehearsed}")
                original = PreparedMigration.capture(held, self.installed)
                backup, selected_route = self.backed_up(selected)
                migration = original.extended(BackedUpMigration, backup=backup)
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
                passed = migration.carry(self.pending, self.staged, self.tree)
                passed.files.require_rehearsal(rehearsal.files)
                self.publish(migration.backup, selected_route)
                holder.close()
                maintenance.clear(held)
                services = self.staged.restart_services()
            except BaseException as error:
                print(f"install: install failed ({type(error).__name__}: {error}); "
                      "restoring original installation", file=sys.stderr)
                # A running service writes the root (the connector its passes), so none may
                # run while the backup is restored; they start again on the original runtime.
                stopped = HostService.stop_running()
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
                self.installed.publish(self.root)
                holder.close()
                maintenance.clear(restored_hold)
                self.installed.start_services(stopped)
                started = self.installed.start(self.root, running)
                print(f"install: restored after failure ({type(error).__name__}: {error}); "
                      f"original runtime selected; owners: {started}; "
                      f"backup: {migration.backup.description}",
                      file=sys.stderr)
                raise InstallRolledBack(error, started, migration.backup.description,
                                        runtime=self.installed.runtime) from error
            # Installed: the carries ran and the runtime is published. Starting the owners it
            # stopped is reported per owner and never rolls the install back.
            report.writer = RuntimeWriter(self.root, self.staged, InstalledProgram())
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

    def live(self, rehearsal: UnchangedRehearsal, report: InstallReport) -> UnchangedRun:
        self.installed.publish(self.root)
        report.writer = RuntimeWriter(self.root, self.installed, InstalledProgram())
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

    @staticmethod
    def initial_writer(root: Path, installed: Stack) -> RootWriter:
        """A fresh bus has nowhere to publish a start message yet."""
        return UnpostedWriter()

    def rehearsal(self) -> FreshRehearsal:
        return FreshRehearsal()

    def live(self, rehearsal: FreshRehearsal, report: InstallReport) -> CreatedRun:
        self.staged.commands.run("git", "-C", self.checkout, "merge", "--ff-only", "-q",
                                 self.target)
        Checkout(self.checkout).prepare()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        wire(self.root).registry.prepare_native_root()
        Bus.install(Bus.path_in(self.root))
        report.writer = RuntimeWriter(self.root, self.staged, InstalledProgram())
        backup, selected_route = self.backed_up(())
        try:
            route = self.publish(backup, selected_route)
            services = self.staged.restart_services()
        except BaseException:
            backup.restore_route()
            self.installed.publish(self.root)
            raise
        backup.retain(self.spill_dir)
        return CreatedRun(services, route)


class RetiringInstall(FreshInstall):
    """The bus at the root is runtime state the target cannot carry: its owners stop, it
    moves aside intact, and a fresh bus takes its place."""

    def rehearsal(self) -> RetiringRehearsal:
        return RetiringRehearsal()

    def live(self, rehearsal: FreshRehearsal, report: InstallReport) -> RetiredRun:
        running = self.installed.running_owners(self.root)
        self.installed.owners(self.root, StopCliCommand, running)
        retired = self.root.with_name(f"{self.root.name}.retired-{time.strftime('%Y%m%dT%H%M%S')}")
        self.root.rename(retired)
        return super().live(rehearsal, report).extended(
            RetiredRun, stopped=tuple(running), retired=retired)
