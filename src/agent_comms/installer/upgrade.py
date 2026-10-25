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
import os
import shutil
import sys
import tempfile
import time
import tomllib
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, ExitStack, contextmanager
from dataclasses import dataclass, field, fields, replace
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, ClassVar, Self

from agent_comms.active_route import (
    ActiveRoute,
    RouteFile,
    RouteFileSelection,
    active_route_path,
    route_directory,
)
from agent_comms.bus import Bus
from agent_comms.cli_bridges.shared import BridgeResume, Serve, SessionBridgeResumes
from agent_comms.cli_commands import (
    BusReadBack,
    CliCommand,
    PlannedReconcileCliCommand,
    ReadBackCliCommand,
    ReconcileStoresCliCommand,
    StartCliCommand,
    StopCliCommand,
    TargetActionCliCommand,
)
from agent_comms.cli_result import CliResult
from agent_comms.comms import wire
from agent_comms.coordination_errors import CoordinationReadUnavailable
from agent_comms.declared_family import DeclaredFamily
from agent_comms.field_codec import PathText
from agent_comms.typed_table import WriterOwnedStore
from agent_comms.host_services import HostService, RestartedService
from agent_comms.install_choices import InstallerChoice, RunMode
from agent_comms.installer.backup import (
    BackupReport,
    RestoreSpace,
    SavedFile,
    StoreBackup,
    bus_files,
    settle_bus_files,
)
from agent_comms.installer.carry import STEPS, CarryChain
from agent_comms.installer.commands import InstallCommands
from agent_comms.installer.declared_uv import DeclaredUv
from agent_comms.installer.interpreter_operation import (
    AbortMaintenancePhase,
    AfterMigrationFileScanPhase,
    BackupRetirePhase,
    BeforeMigrationFileScanPhase,
    BeginMaintenancePhase,
    BuildPhase,
    ClearMaintenancePhase,
    ConfirmBridgePhase,
    CopyPhase,
    DiscoverAndPauseBridgePhase,
    GitPhase,
    InstallClock,
    InstallRunPhase,
    InstalledProgram,
    LiveCheckoutPreparePhase,
    LivePhase,
    OutgoingRuntimeMaintenancePhase,
    PhaseTiming,
    PracticePhase,
    PreparePhase,
    ProbeProgram,
    ProcessScanPhase,
    ReadBackVerifyPhase,
    ReleaseBridgePhase,
    RestoredCheckoutPreparePhase,
    RestoreSpaceBackupPhase,
    RetargetMaintenancePhase,
    RouteAndRuntimePublicationPhase,
    RuntimeProbe,
    RuntimePublicationPhase,
    StartingReportPublicationPhase,
    StartServicesPhase,
    StopServicesPhase,
    StoresAndRouteBackupPhase,
    SubjectPhase,
    TargetProgram,
)
from agent_comms.installer.owner_starts import OwnerStart
from agent_comms.installer.practice import (
    PracticeCopy,
    PracticeMeasurement,
    RehearsalHome,
    RuntimeHome,
)
from agent_comms.installer.probes.carry import (
    Carried,
    Carry,
    CarryOperation,
    CarryProbe,
    FirstPendingOperation,
    MigrateOperation,
    RetireInputsOperation,
    WriterOwnedOpeningProbe,
)
from agent_comms.installer.probes.commands import (
    CliCommandProbe,
    CliOutput,
    CliRefusedError,
    CliUndecodableError,
)
from agent_comms.installer.probes.harnesses import InstallResult
from agent_comms.installer.probes.imports import ClientImportsProbe
from agent_comms.installer.probes.native import PrepareNativeProbe, PrepareProducerDigestsProbe
from agent_comms.installer.probes.owners import OwnerActionsProbe, RunningOwnersProbe
from agent_comms.installer.probes.publication import PublicationProbe, SelectedRouteProbe
from agent_comms.installer.probes.services import ServingProbe
from agent_comms.installer.refusals import InstallRolledBack, UpgradeRefused
from agent_comms.installer.reporting import (
    RootWriter,
    RuntimeWriter,
    UnpostedWriter,
)
from agent_comms.installer.runtime_resolution import RuntimeResolution, StackProject
from agent_comms.installer.runtime_wire import CrossesRuntimes
from agent_comms.installer.refusals import RuntimeWireChanged
from agent_comms.launch_runtime import LaunchRuntime, RestartEnvironment
from agent_comms.locked_store import BusFile, DeclaredFile, RootFile, UndeclaredFile
from agent_comms.maintenance_barrier import (
    MaintenanceBarrier,
    MaintenanceOperator,
    MaintenanceState,
)
from agent_comms.native_build_inputs import NativeBuildInputs
from agent_comms.native_package import (
    NativePackageStore,
    pinned_package_for,
)
from agent_comms.recorded_bus import RecordedBusIdentity
from agent_comms.registry_store import RegistryStore
from agent_comms.repo import Checkout, DetachedStart, Repo, ScratchCheckout
from agent_comms.root_runtime import RootRuntime, RootRuntimeStore
from agent_comms.runtime_processes import RuntimeProcesses
from agent_comms.storage_rules import StorageRule

if TYPE_CHECKING:
    from agent_comms.installer.launch import InstallReport

HOME = Path.home()


@dataclass(frozen=True, kw_only=True)
class OwnersPhase(SubjectPhase):
    """One concurrent batch of owner requests, measured by its elapsed wall time."""

    command: type[CliCommand]

    @property
    def subject(self) -> str:
        return self.command.declared_name


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
    original_root: Path, external: tuple[type[WriterOwnedStore], ...] = (),
) -> RehearsalFiles:
    """Only the files a step names as rewritten may differ from ``before``."""
    after = bus_files(root, original_root, external)
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
class Extendable:
    """A record a subclass extends with facts of its own; the shared ones carry over."""

    def extended[Record](self, kind: type[Record], **facts: object) -> Record:
        return kind(**({each.name: getattr(self, each.name) for each in fields(self)} | facts))


@dataclass(frozen=True)
class CarriedPass(Extendable):
    """What a rehearsal and a live install share: the steps, the files they changed, and the
    bus read back."""

    steps: tuple[StepRun, ...]
    files: RehearsalFiles
    read: BusReadBack
    retired_inputs: tuple[StepRun, ...] = field(default=(), kw_only=True)

    def after_acceptance(self, pending: tuple[Carry, ...], stack: TargetStack) -> Self:
        return replace(self, retired_inputs=tuple(
            StepRun(step.declared_name, retired) for step, retired in
            zip(pending, stack.call_steps(pending, RetireInputsOperation()), strict=True)
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
class UncopiedPractice:
    copy: PracticeMeasurement = PracticeMeasurement(0)


@dataclass(frozen=True)
class UncopiedRehearsal(RehearsedFiles, UncopiedPractice):
    """A read of the live root found no carry pending and no store to reconcile, so no
    practice copy was taken and the live pass may rewrite nothing. Its own planned
    reconcile, under the hold, still refuses before any write if it names a file."""

    files: RehearsalFiles = field(default_factory=RehearsalFiles.none)
    inputs: tuple[Annotated[Path, PathText], ...] = ()


@dataclass(frozen=True)
class UnchangedRehearsal(Rehearsal, UncopiedPractice):
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
    phases: list[PhaseTiming] = field(kw_only=True)
    """Every phase shares the one clock until installation and cleanup end."""

    finished: ClassVar[str] = "Install rehearsed"

    def live_lines(self) -> tuple[str, ...]:
        return ()

    def lines(self) -> tuple[str, ...]:
        slowest = sorted(self.phases, key=lambda timed: timed.seconds, reverse=True)[:5]
        return (*super().lines(),
                f"Phases: {len(self.phases)} in "
                f"{sum(timed.seconds for timed in self.phases):.1f}s; slowest:",
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

    def live_lines(self) -> tuple[str, ...]:
        return self.live.lines()

    def lines(self) -> tuple[str, ...]:
        return (*super().lines(), *self.live_lines(), *self.processes.lines(),
                *(line for outcome in self.bridge_resumes for line in outcome.lines()))


@dataclass(frozen=True)
class UnresumedUpgrade(InstalledUpgrade):
    """Installed, with session bridges paused for the swap that did not come back: the
    install exits non-zero after reporting it."""

    finished: ClassVar[str] = "Install finished; session bridges not resumed"


@dataclass(frozen=True)
class ServicesStopped:
    """Stopped services return to their original stack until a start succeeds."""

    services: tuple[HostService, ...]
    give_back: ExitStack

    def start_on(self, stack: Stack) -> tuple[RestartedService, ...]:
        restarted = stack.start_services(self.services)
        self.give_back.pop_all()
        return restarted


@dataclass
class OwnersStopped:
    """The agent owners a live install stopped, from its start message on. Until a
    published runtime takes them over, every exit starts the same owners again on the
    installed runtime, and their start outcomes join the install's report."""

    stopped: list[str] = field(default_factory=list)
    started: tuple[OwnerStart, ...] = ()
    give_back: ExitStack = field(default_factory=ExitStack)

    def stop(self, stack: Stack, root: Path) -> None:
        """Each owner is remembered once, before its batch, so a partly refused stop still
        starts again every owner it stopped, and one restarted since is not started twice."""
        running = stack.running_owners(root)
        self.stopped.extend(name for name in running if name not in self.stopped)
        for outcome in stack.owners(root, StopCliCommand, running):
            outcome.require_success(UpgradeRefused)

    def start_on(self, stack: Stack, root: Path) -> tuple[OwnerStart, ...]:
        self.started = stack.owners(root, StartCliCommand, self.stopped)
        return self.started

    def published_on(self, stack: Stack, root: Path) -> tuple[OwnerStart, ...]:
        """The installed runtime is no longer published, so it never takes these owners
        back, even when a start on ``stack`` fails."""
        self.give_back.pop_all()
        return self.start_on(stack, root)

    def retired(self) -> None:
        """The owners belong to a bus moved aside; nothing starts them again."""
        self.give_back.pop_all()

    def release(self) -> None:
        """No hold was taken with these owners, so none is cleared."""

    def keep(self) -> None:
        """Nothing starts on the root again."""
        self.give_back.pop_all()

    @contextmanager
    def held_on_failure(self) -> Iterator[None]:
        """A raise inside leaves a root its runtime may not open: no owner starts on it."""
        try:
            yield
        except BaseException:
            self.keep()
            raise

    def lines(self) -> tuple[str, ...]:
        return tuple(line for owner in self.started for line in owner.lines())


@dataclass
class HeldOwnersStopped(OwnersStopped):
    """Owners the outgoing runtime stopped under the install's one maintenance hold, which lasts
    to its release, before anything starts on the root again, or to the install's exit."""

    hold: ExitStack = field(default_factory=ExitStack)

    def release(self) -> None:
        """Clears the hold once; a later call finds nothing left to clear."""
        self.hold.close()

    def keep(self) -> None:
        """The hold stays draining too."""
        self.hold.pop_all()
        super().keep()


@dataclass(frozen=True)
class Stack(Extendable):
    """One built installation and the source checkout containing its install tools."""

    checkout: Path
    commands: InstallCommands
    home: Path = HOME
    runtime: LaunchRuntime = field(default_factory=LaunchRuntime.installed)
    program: ProbeProgram = InstalledProgram()
    """How this runtime's worker reaches the operation family: the outgoing runtime answers
    from its own installed installer; a runtime built from the target declares it too,
    with the tree's carry steps beside it."""

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
        """The installer runs only the uv its checkout declares; a different one installs the
        declared uv in its own place before any lock is written."""
        if not os.access(self.uv, os.X_OK):
            raise UpgradeRefused(f"No uv executable at {self.uv}; the installer runs only that uv")
        declared = DeclaredUv.of(self.checkout / "pyproject.toml", self.uv)
        if declared.accepts(self.commands.run(self.uv, "--version")):
            return
        with tempfile.TemporaryDirectory(dir=self.uv.parent, prefix=".uv-") as staging:
            self.commands.run(self.uv, "pip", "install", "--quiet", "--target", staging,
                              "--python", self.python, str(declared.requirement))
            fetched = Path(staging) / "bin/uv"
            reported = self.commands.run(fetched, "--version")
            if not declared.accepts(reported):
                raise UpgradeRefused(f"Fetched {reported.strip()}; {declared.requirement} declared")
            os.replace(fetched, self.uv)

    def require_runtime_wire(self) -> None:
        """Compare the target's wire with the source stamped into this installed wheel.

        Isolated Python ignores the target installer's PYTHONPATH: the old wheel, not
        the current checkout or its build metadata, owns the outgoing Core identity.
        """
        commit = self.commands.run(
            self.python, "-I", "-c",
            "from agent_comms.package_stamp import STAMP; print(STAMP.commit())",
        ).strip()
        paths = tuple(dict.fromkeys(member.path for member in CrossesRuntimes.declarations()))
        present = self.commands.git(self.checkout, "ls-tree", "--name-only", "-r", commit,
                                    "--", *paths).splitlines()
        sources = {path: self.commands.git(self.checkout, "show", f"{commit}:{path}")
                   for path in present}
        drift = CrossesRuntimes.drift_sources(sources)
        if drift:
            raise RuntimeWireChanged(commit, drift)

    def resolve(self) -> None:
        """Refresh declared source packages while retaining the committed release pins."""
        with self.lock.with_name("pyproject.toml").open("rb") as definition:
            project = StackProject.model_validate(tomllib.load(definition))
        upgrades = tuple(argument for name in project.tool.uv.sources
                         for argument in ("--upgrade-package", name))
        self.commands.run(self.uv, "lock", "--project", "stack", *upgrades,
                          "--python", "3.14", cwd=self.checkout)

    @staticmethod
    def build(checkout: Checkout, commands: InstallCommands, home: Path = HOME) -> TargetStack:
        """Resolve current mains once, then build or reuse their immutable installation."""
        with BuildPhase().running(commands.clock):
            source = Stack(checkout.path, commands, home, runtime=LaunchRuntime.running())
            source.require_uv()
            source.resolve()
            core = commands.git(checkout.path, "rev-parse", "HEAD").strip()
            resolution = hashlib.sha256(source.lock.read_bytes()).hexdigest()[:16]
            runtimes = home / ".local/share/agent-comms/runtimes"
            prefix = runtimes / f"core-{core[:9]}-lock-{resolution}"
            built = source.extended(
                TargetStack, runtime=LaunchRuntime(prefix / "bin/python"),
                program=TargetProgram(checkout.path / STEPS.parent))
            if prefix.exists():
                if not RuntimeResolution.at(prefix).matches(source.lock, built, core):
                    raise UpgradeRefused("Existing built runtime differs from its resolved inputs")
                built.stage_native()
                built.prepare_runtime()
                return built
            prefix.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            prefix.mkdir(mode=0o700)
            try:
                commands.run(source.uv, "sync", "--project", "stack", "--locked",
                             "--python", "3.14", "--no-editable", cwd=checkout.path,
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
        core = self.commands.git(self.checkout, "rev-parse", "HEAD").strip()
        scratch = StorageRule().scratch_base
        scratch.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch, prefix=f"tests-{core[:12]}-") as temporary:
            print(self.commands.run(
                self.python, "-m", "pytest", "-q", "-s",
                "-o", "pythonpath=. tools", "-o", "faulthandler_timeout=60",
                "--basetemp", Path(temporary) / "pytest", "-p", "no:cacheprovider",
                *pytest_arguments, cwd=self.checkout, env=self.environment()), end="")

    def probe[Answer](self, probe: RuntimeProbe[Answer]) -> Answer:
        """Every call into this runtime during an install goes to its one worker, which
        imports the runtime once."""
        return self.commands.probe(probe, self.python, cwd=self.home,
                                   env=self.environment(*self.program.import_roots()),
                                   program=self.program)

    def imports(self) -> None:
        self.probe(ClientImportsProbe())

    def stage_native(self) -> tuple[Path, Path]:
        """Prepare a missing artifact once; the checkout holds only its store link."""
        manifest = self.checkout / "stack/pi-native.sha256"
        try:
            NativeBuildInputs(self.checkout).require_pinned(manifest)
        except ValueError as error:
            raise UpgradeRefused(str(error)) from None
        store = NativePackageStore.at_home(self.home)
        native = store.package_for(manifest)
        if not native.is_dir():
            self.commands.run(self.checkout / "stack/native/prepare-pi-native", "--python",
                              self.python, cwd=self.checkout,
                              env=self.environment(self.checkout / "src"))
        store.link(manifest, manifest.parent)
        return native, pinned_package_for(manifest).relative_to(manifest.parent)

    def install_native(self) -> None:
        """Interpreters bind the store artifact; disposable trees contain only links."""
        self.stage_native()
        NativePackageStore.at_home(self.home).link(
            self.checkout / "stack/pi-native.sha256", self.runtime.package_root / "_native")
        self.prepare_runtime()

    def prepare_runtime(self) -> None:
        self.probe(PrepareProducerDigestsProbe())
        self.probe(PrepareNativeProbe())

    def publish(self, root: Path) -> None:
        """The selected interpreter owns console metadata and harness declarations, and
        ``root`` belongs to this stack's runtime from now on."""
        with RuntimePublicationPhase().running(self.commands.clock):
            self.probe(PublicationProbe())
            RootRuntime.publish(root, self.runtime)

    def environment(self, *paths: Path) -> dict:
        """Use the artifact's packages; ``paths`` are searched first, in order (the target
        tree's carry steps, or its source for a native build)."""
        environment = {**self.runtime.environment(os.environ), "HOME": str(self.home)}
        configuration = RestartEnvironment.inherit(environment)
        for path in reversed(paths):
            configuration = configuration.with_import_root(path)
        return configuration.apply_runtime(environment)

    def cli[Answer: CliResult](self, root: Path, command: type[CliCommand[Answer]],
                               *arguments: str) -> Answer:
        """This bus's answer to ``command``, never the route's, decoded as the result the
        command declares."""
        invocation = command.invocation(*arguments)
        return self.cli_reply(CliCommandProbe(("--root", str(root), *invocation)),
                              partial(CliOutput.answer, result=command.result_type()))

    def cli_reply[Answer](self, probe: CliCommandProbe,
                          decode: Callable[[CliOutput], Answer]) -> Answer:
        """One failure/retry policy; CliOutput owns decoding and exit-code validation."""
        verb = f"agent-comms {' '.join(probe.arguments)}"
        for _ in range(240):
            try:
                return decode(self.probe(probe))
            except CliUndecodableError as undecodable:
                raise UpgradeRefused(f"{verb}: undecodable answer: {undecodable}") from undecodable
            except CliRefusedError as refused:
                if not issubclass(refused.failure.exception_type, CoordinationReadUnavailable):
                    raise UpgradeRefused(f"{verb}: {refused}") from refused
                waited = refused
            time.sleep(5)
        raise UpgradeRefused(f"{verb}: still unavailable after 20 minutes: {waited}")

    def running_owners(self, root: Path) -> list[str]:
        """Use the restart request's receiving declaration in the selected runtime."""
        return self.probe(RunningOwnersProbe(root))

    def owners(self, root: Path, command: type[CliCommand],
               names: list[str]) -> tuple[OwnerStart, ...]:
        """Run and decode the owner selection in the runtime that owns its CLI."""
        if not names:
            return ()
        with OwnersPhase(command=command).running(self.commands.clock):
            invocation = TargetActionCliCommand.invocation(
                "--operation", command.declared_name, "--targets", *names)
            return self.cli_reply(OwnerActionsProbe(("--root", str(root), *invocation)),
                                  partial(CliOutput.decoded, result=tuple[OwnerStart, ...]))

    @contextmanager
    def services_stopped(self, restore_on: Stack) -> Iterator[ServicesStopped]:
        """The restart is registered before the stop, so a stop interrupted after systemd
        acted (a SIGTERM to an installer inside the stopped unit) still starts them again."""
        stopped = HostService.instances()
        with ExitStack() as give_back:
            give_back.callback(restore_on.start_services, stopped)
            with StopServicesPhase().running(self.commands.clock):
                HostService.stop_all(stopped)
            yield ServicesStopped(stopped, give_back)

    def start_services(self, stopped: tuple[HostService, ...]) -> tuple[RestartedService, ...]:
        with StartServicesPhase().running(self.commands.clock):
            services = tuple(service.start_onto(self.python) for service in stopped)
            HostService.start_enabled()
            self.probe(ServingProbe())
            return services

    def require_opens(self, root: Path) -> None:
        """A restored root opens under this runtime's declarations, the check every opening
        makes (TableSchema.require_schema), answered by this runtime's own CLI: install 13
        released a half-restored ledger that every later opening refused (install 14,
        2026-10-24)."""
        differing = self.cli(root, PlannedReconcileCliCommand).rewritten
        if differing:
            raise UpgradeRefused(
                f"Rollback left stores that differ from {self.runtime.prefix.name}'s "
                f"declarations: {', '.join(map(str, differing))}; maintenance stays held")

    def verify(self, root: Path) -> BusReadBack:
        """What the carried bus answers through the CLI of the runtime that serves it next."""
        with ReadBackVerifyPhase().running(self.commands.clock):
            return self.cli(root, ReadBackCliCommand)


@dataclass(frozen=True)
class TargetStack(Stack):
    """The built target owns carry declarations and the locks its worker borrows."""

    program: TargetProgram = field(kw_only=True)

    @contextmanager
    def borrowing(self, stores: tuple[type[WriterOwnedStore], ...], root: Path,
                  original_root: Path) -> Iterator[TargetStack]:
        """The installer retains file locks through rollback; workers only borrow them."""
        if not stores:
            yield self
            return
        with ExitStack() as lifetime:
            locks = tuple(lifetime.enter_context(store.retaining(store.located(root, original_root)))
                          for store in stores)
            program = self.program.borrowing(locks)
            lifetime.callback(self.commands.close_program, self.python, program)
            yield replace(self, program=program)

    def call_steps[Answer](self, steps: tuple[Carry, ...],
                           operation: CarryOperation[Answer]) -> tuple[Answer, ...]:
        """Run the target's declared passes in order, in its own worker."""
        if not steps:
            return ()
        return self.probe(CarryProbe(operation, steps, self.program.inherited_locks))


@dataclass(frozen=True)
class TargetTree(AbstractContextManager):
    """The temporary worktree of the target revision the install runs from."""

    repo: Path
    scratch: Path
    revision: str

    @property
    def path(self) -> Path:
        return self.scratch / f"tree-{self.revision[:12]}"

    @property
    def steps_package(self) -> Path:
        """Where the tree's carry steps import from; they ship in no runtime."""
        return self.path / STEPS.parent

    def source_paths(self) -> tuple[Path, Path]:
        """What the installer imports from when an older runtime runs it: the tree's
        ``src``, then its carry steps' package."""
        return self.path / "src", self.steps_package

    def add(self) -> None:
        ScratchCheckout.add(self.repo, self.path, DetachedStart(self.revision))

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
    clock: InstallClock

    @classmethod
    @contextmanager
    def of(cls, root: Path, installer: InstallerChoice, spill_dir: Path, clock: InstallClock
           ) -> Iterator[PracticeBus]:
        parent = installer.scratch(StorageRule.load(root))
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        spill_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (
            tempfile.TemporaryDirectory(dir=parent, prefix="practice-") as scratch,
            tempfile.TemporaryDirectory(dir=spill_dir, prefix="practice-carry-") as spill,
        ):
            yield cls(Path(scratch), PracticeCopy(root, Path(scratch) / "bus"), root,
                      Path(spill), clock)

    def pending(self, chain: CarryChain, staged: TargetStack) -> tuple[type[Carry], ...]:
        """Read-only dry runs in one worker stop at the first step that says work remains."""
        unfinished = chain.unfinished_at(self.original_root).steps
        if not unfinished:
            return ()
        steps = tuple(step(self.original_root, self.original_root, self.spill_dir)
                      for step in unfinished)
        (position,) = staged.call_steps(steps, FirstPendingOperation())
        return unfinished[position:]

    def select(self, pending: tuple[Carry, ...], reconciled: tuple[Path, ...]) -> tuple[Path, ...]:
        # This is the one input union consumed by both rehearsal and live backup.
        paths = tuple(sorted({
            RegistryStore.at(self.original_root).path,
            RootRuntimeStore.path_in(self.original_root),
            *(self.original_root / path for path in reconciled),
            *(path for step in pending for path in step.input_files()),
        }))
        with CopyPhase().running(self.clock):
            self.copy.include(paths)
            self.copy.retain(paths)
        return self.copy.copied_names


def upgrade(checkout: Path, root: Path, target: str, installer: InstallerChoice, mode: RunMode,
            retire: bool, spill_dir: Path, scratch: Path,
            harnesses: Callable[[Stack], InstallResult], report: InstallReport) -> Upgrade:
    """``scratch`` holds the target's worktree the launcher runs this source from; this
    run owns it and removes it. A live run ends by registering the ``harnesses``."""
    commands = InstallCommands()
    tree = TargetTree(checkout, scratch, target)
    bridges = report.bridges
    with InstallRunPhase().running(commands.clock), commands, report.running(tree, commands.clock):
        with PreparePhase().running(commands.clock):
            StoreBackup.complete_interrupted(root, spill_dir)
            current = commands.git(checkout, "rev-parse", "HEAD").strip()
            with commands.timing(GitPhase(
                checkout, ("merge-base", "--is-ancestor", current, target),
            )):
                follows = Repo.descends(checkout, current, target)
            if not follows:
                raise UpgradeRefused(f"{target[:12]} does not follow the checkout's {current[:12]}")
            if commands.git(checkout, "status", "--porcelain",
                            "--untracked-files=no").strip():
                raise UpgradeRefused("The checkout has local changes")
            try:
                chain = CarryChain.at(tree.path)
            except ValueError as error:
                raise UpgradeRefused(str(error)) from None
            member = Install.member(root, retire)
            installed = Stack(tree.path, commands)
            if not installed.python.exists():
                raise UpgradeRefused(f"No installed stack at {installed.python}")
            installed.require_runtime_wire()
            installed.require_uv()
        staged = Stack.build(ScratchCheckout(tree.path), commands)
        with StartingReportPublicationPhase().running(commands.clock):
            report.starting(mode.destination(member.initial_writer(root, installed)), target)
        with mode.owners_stopped(member, root, installed, staged, bridges) as report.owners:
            with PracticePhase().running(commands.clock):
                install, rehearsal = member.rehearsed(checkout, root, current, target, chain,
                                                      installed, staged, tree, installer,
                                                      spill_dir)
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
    chain: CarryChain
    pending: tuple[Carry, ...]
    installed: Stack
    staged: TargetStack
    tree: TargetTree
    installer: InstallerChoice
    spill_dir: Path

    @staticmethod
    def initial_writer(root: Path, installed: Stack) -> RootWriter:
        """The outgoing runtime posts until the target's runtime takes the bus over, unless
        the route file refuses it as a borrower; install's own route step replaces that file."""
        return RouteFile.read(active_route_path()).outgoing_writer(
            RuntimeWriter(root, installed), UnpostedWriter())

    def backed_up(self, files: tuple[Path, ...]) -> tuple[StoreBackup, RouteFileSelection]:
        with StoresAndRouteBackupPhase().running(self.staged.commands.clock):
            backup = StoreBackup.take(self.root, active_route_path(), files, self.spill_dir)
            with route_directory(backup.route):
                backup.require_route_revision()
                selected = self.staged.probe(SelectedRouteProbe())
            return backup, selected

    def checkout_target(self) -> None:
        """The live checkout moves to the target before anything runs from it."""
        with LiveCheckoutPreparePhase().running(self.staged.commands.clock):
            self.staged.commands.git(self.checkout, "merge", "--ff-only", "-q", self.target)
            Checkout(self.checkout).prepare()

    def publish(self, backup: StoreBackup, selected: RouteFileSelection) -> str:
        """The converted bus's route is ready before new runtime entrypoints open it."""
        with RouteAndRuntimePublicationPhase().running(self.staged.commands.clock):
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
    @contextmanager
    def bridges_paused(cls, installed: Stack, staged: TargetStack,
                       bridges: SessionBridgeResumes) -> Iterator[None]:
        """Session bridges close their bus resources before the install holds the bus. A
        bridge paused under the hold dies on its exit-time registry write and never answers
        (installs 21 and 22, 2026-10-25: MaintenanceHeldError, then an empty pause reply)."""
        with ExitStack() as paused:
            with DiscoverAndPauseBridgePhase().running(staged.commands.clock):
                paused.enter_context(bridges.holding(Serve.running(installed.home)))
            yield

    @classmethod
    @contextmanager
    def owners_kept(cls, root: Path, installed: Stack,
                    staged: TargetStack) -> Iterator[OwnersStopped]:
        """Every owner keeps running: none runs at the root, or the run touches only a copy."""
        yield OwnersStopped()

    @classmethod
    def owners_stopped(cls, root: Path, installed: Stack,
                       staged: TargetStack) -> AbstractContextManager[OwnersStopped]:
        return cls.owners_kept(root, installed, staged)

    @classmethod
    @abstractmethod
    def rehearsed(cls, checkout: Path, root: Path, current: str, target: str,
                  chain: CarryChain, installed: Stack, staged: TargetStack, tree: TargetTree,
                  installer: InstallerChoice, spill_dir: Path) -> tuple[Install, Rehearsal]:
        """This member's install, with what is pending, and its rehearsal."""

    def reported(self, rehearsal: Rehearsed) -> RehearsedUpgrade:
        return RehearsedUpgrade(
            self.current, self.target, self.root,
            tuple(step.declared_name for step in self.pending), rehearsal,
            phases=self.staged.commands.clock.phases)

    def finished(self, rehearsal: Rehearsed, bridges: SessionBridgeResumes,
                 harnesses: Callable[[Stack], InstallResult], report: InstallReport) -> InstalledUpgrade:
        with LivePhase().running(self.staged.commands.clock):
            rehearsed = self.reported(rehearsal)
            try:
                live_run = self.install(rehearsal, bridges, report)
                self.chain.finish_at(self.root)
                if self.installed.python != self.staged.python:
                    self.staged.commands.close_runtime(self.installed.python)
                installed_harnesses = harnesses(self.staged)
                with ProcessScanPhase().running(self.staged.commands.clock):
                    processes = RuntimeProcesses.observe(self.staged.runtime)
            finally:
                # Includes rollback, a failed pause, and failures after the live run.
                with ConfirmBridgePhase().running(self.staged.commands.clock):
                    bridges.confirm()
            finished = InstalledUpgrade(
                rehearsed.source, rehearsed.target, rehearsed.root, rehearsed.pending,
                rehearsed.rehearsal, live_run, installed_harnesses,
                phases=self.staged.commands.clock.phases, processes=processes,
                bridge_resumes=tuple(bridges.outcomes))
            return (finished.extended(UnresumedUpgrade)
                    if bridges.failures else finished)

    def install(self, rehearsal: Rehearsed, bridges: SessionBridgeResumes,
                report: InstallReport) -> LiveRun:
        """Bridges paused before the hold (bridges_paused) exec in place once live() publishes
        or rolls back; the installed pointer, not a copied target version, selects each
        successor. Each serve retains its recorded owner's PID and start time."""
        try:
            return report.record_live(self.live(rehearsal, report))
        finally:
            with ReleaseBridgePhase().running(self.staged.commands.clock):
                bridges.release()

    @abstractmethod
    def live(self, rehearsal: Rehearsed, report: InstallReport) -> LiveRun: ...


class StopsOwners:
    """A bus whose agent owners run: they stop the moment the install announces its start,
    not after the rehearsal, and start again on the installed runtime on any exit that
    no published runtime took them over from."""

    @classmethod
    @contextmanager
    def owners_stopped(cls, root: Path, installed: Stack,
                       staged: TargetStack) -> Iterator[OwnersStopped]:
        owners = OwnersStopped()
        with owners.give_back:
            owners.give_back.callback(owners.start_on, installed, root)
            owners.stop(installed, root)
            yield owners


class ExistingBusInstall[Rehearsed: Rehearsal](Install[Rehearsed]):
    """A bus is already at the root. When a carry step exists or a store's schema differs
    from the target's, one practice copy of it answers what is pending and carries the
    rehearsal; otherwise nothing could change, and no copy is taken."""

    @classmethod
    @contextmanager
    def owners_stopped(cls, root: Path, installed: Stack,
                       staged: TargetStack) -> Iterator[OwnersStopped]:
        """One hold, one stop and one retarget per install, all at its start. The outgoing
        runtime stops the owners: their stores still hold its declarations until this
        install carries them, and the target refuses every store whose shape it has not
        carried yet (install of 7dbd43736, 2026-10-25: #3193 rekeyed CurrentNativeCursor,
        so the target's stop refused the coordinator file before any carry ran). Only then
        does the hold move to the target, and from there to its release it refuses every
        other writer and every owner start, so nothing writes the root before the backup."""
        clock = staged.commands.clock
        with OutgoingRuntimeMaintenancePhase().running(clock):
            RootRuntimeStore.at(root).adopt(installed.runtime)
        maintenance = MaintenanceBarrier(RegistryStore.at(root).path)
        owners = HeldOwnersStopped()
        with owners.give_back:
            owners.give_back.callback(owners.start_on, installed, root)
            with owners.hold:
                with BeginMaintenancePhase().running(clock):
                    held = maintenance.begin(MaintenanceOperator.current())
                owners.hold.callback(
                    lambda: maintenance.clear(maintenance.held_by(MaintenanceOperator.current())))
                with held.holding():
                    owners.stop(installed, root)
                with RetargetMaintenancePhase().running(clock):
                    held = maintenance.retarget(held, staged.runtime)
                owners.hold.enter_context(held.holding())
                yield owners

    @classmethod
    def rehearsed(cls, checkout: Path, root: Path, current: str, target: str,
                  chain: CarryChain, installed: Stack, staged: TargetStack, tree: TargetTree,
                  installer: InstallerChoice, spill_dir: Path) -> tuple[Install, Rehearsal]:
        CarryChain.require_crossed(checkout, root, target)
        with PracticeBus.of(root, installer, spill_dir, staged.commands.clock) as practice:
            pending = practice.pending(chain, staged)
            reconciled = staged.cli(
                root, PlannedReconcileCliCommand, "--carrying",
                *(store.declared_name for store in Carry.writer_declarations(pending)),
            ).rewritten
            install = cls.of(checkout, root, current, target, chain, pending, reconciled,
                             installed, staged, tree, installer, spill_dir)
            if pending or reconciled:
                return install, install.rehearsal(practice, reconciled)
            return install, install.uncopied()

    @classmethod
    def of(cls, checkout: Path, root: Path, current: str, target: str, chain: CarryChain,
           pending: tuple[type[Carry], ...], reconciled: tuple[Path, ...],
           installed: Stack, staged: TargetStack, tree: TargetTree,
           installer: InstallerChoice, spill_dir: Path) -> ExistingBusInstall:
        """A bus already carried, on the target, built from the installed lock needs nothing."""
        unchanged = not pending and not reconciled and current == target and RuntimeResolution.installed().matches(
            staged.lock, staged, target)
        member = (ChangedInstall, UnchangedInstall)[unchanged]
        steps = tuple(step(root, root, spill_dir) for step in pending)
        return member(checkout, root, current, target, chain, steps, installed, staged,
                      tree, installer, spill_dir)

    @abstractmethod
    def rehearsal(self, practice: PracticeBus, reconciled: tuple[Path, ...]) -> Rehearsed: ...

    @abstractmethod
    def uncopied(self) -> Rehearsed:
        """This member's rehearsal when nothing on the bus could change."""


@dataclass(frozen=True)
class PreparedMigration(Extendable):
    """The held bus as the outgoing runtime read it, before any migration."""

    hold: MaintenanceState
    before: dict[DeclaredFile | UndeclaredFile, dict[Path, SavedFile]]
    read: BusReadBack
    original_root: Path
    writer_stores: tuple[type[WriterOwnedStore], ...] = ()

    @property
    def root(self) -> Path:
        return Path(self.hold.root)

    @classmethod
    def capture(cls, hold: MaintenanceState, outgoing: Stack,
                steps: tuple[Carry, ...] = ()) -> PreparedMigration:
        """Snapshot the held bus and the chain's declared external locations.
        Only bus resources settle; external bytes reach backup without a prior write."""
        root = Path(hold.root)
        original_root = next((step.original_root for step in steps), root)
        writer_stores = Carry.writer_declarations(steps)
        with BeforeMigrationFileScanPhase().running(outgoing.commands.clock):
            settle_bus_files(root, original_root, writer_stores)
            before = bus_files(root, original_root, writer_stores)
        return cls(hold, before, outgoing.verify(root), original_root, writer_stores)

    def carry(self, pending: tuple[Carry, ...], staged: TargetStack) -> CarriedPass:
        """One migration sequence for rehearsal and the backed-up live bus."""
        steps = [StepRun(step.declared_name, carried) for step, carried in zip(
            pending, staged.call_steps(pending, MigrateOperation(apply=True)), strict=True)]
        staged.probe(WriterOwnedOpeningProbe(self.root, self.original_root, self.writer_stores))
        reconciled = staged.cli(self.root, ReconcileStoresCliCommand)
        steps.append(StepRun(ReconcileStoresCliCommand.declared_name, Carried(
            len(reconciled.rewritten), rewritten=reconciled.rewritten)))
        declared = tuple(steps)
        with AfterMigrationFileScanPhase().running(staged.commands.clock):
            files = carried(self.root, self.before, declared,
                            RehearsalFiles.declared(declared), self.original_root, self.writer_stores)
        passed = CarriedPass(declared, files, staged.verify(self.root))
        passed.require_read(self.read)
        return passed


class ChangedInstall(ExistingBusInstall[RehearsedFiles]):
    def rehearsal(self, practice: PracticeBus, reconciled: tuple[Path, ...]) -> CarriedRehearsal:
        copied = practice.copy.path
        home = practice.scratch / "home"
        RehearsalHome(home).prepare()
        (home / ".claude").symlink_to(Path.home() / ".claude")
        stack = replace(self.staged, home=home)
        pending = tuple(replace(step, root=copied, spill_dir=practice.spill_dir)
                        for step in self.pending)
        inputs = practice.select(self.pending, reconciled)
        self.installed.running_owners(copied)
        maintenance = MaintenanceBarrier(RegistryStore.at(copied).path)
        hold = maintenance.retarget(maintenance.begin(MaintenanceOperator.current()), stack.runtime)
        with hold.holding(), stack.borrowing(
            Carry.writer_declarations(pending), copied, self.root,
        ) as stack:
            migration = PreparedMigration.capture(hold, self.installed, pending)
            passed = migration.carry(pending, stack)
            passed = passed.after_acceptance(pending, stack)
        maintenance.clear(hold)
        return passed.extended(CarriedRehearsal, copy=practice.copy.measurement, inputs=inputs)

    def uncopied(self) -> UncopiedRehearsal:
        return UncopiedRehearsal()

    def live(self, rehearsal: RehearsedFiles, report: InstallReport) -> CarriedRun:
        expected = frozenset(rehearsal.files.names) | frozenset(rehearsal.inputs)
        selected = tuple(owner.source_path(self.root, self.root, resource.path)
                         for resource, owner in RootFile.named_files(
                             self.root, expected, self.root).items())
        with RestoreSpaceBackupPhase().running(self.staged.commands.clock):
            restore = RestoreSpace.measure(max(
                rehearsal.files.restore_bytes,
                sum(path.stat().st_size for path in selected if path.exists()),
            ), self.root.parent)
            restore.require()
        with self.staged.borrowing(
            Carry.writer_declarations(self.pending), self.root, self.root,
        ) as staged, staged.services_stopped(self.installed) as stopped:
            # The install's start holds the root and stopped its owners (owners_stopped).
            maintenance = MaintenanceBarrier(RegistryStore.at(self.root).path)
            holder = ExitStack()
            try:
                try:
                    planned = self.staged.cli(
                        self.root, PlannedReconcileCliCommand, "--carrying",
                        *(store.declared_name for store in Carry.writer_declarations(self.pending)),
                    )
                    unrehearsed = sorted(
                        frozenset(planned.rewritten) - frozenset(rehearsal.files.names))
                    if unrehearsed:
                        raise UpgradeRefused(
                            f"Reconcile would rewrite files the rehearsal did not: {unrehearsed}")
                    migration = PreparedMigration.capture(
                        maintenance.held_by(MaintenanceOperator.current()), self.installed,
                        self.pending)
                    backup, selected_route = self.backed_up(selected)
                except BaseException as error:
                    with AbortMaintenancePhase().running(self.staged.commands.clock):
                        report.owners.release()
                    print(
                        f"install: aborted before the backup ({type(error).__name__}: {error}); "
                        "maintenance cleared; owners start on the installed runtime",
                        file=sys.stderr)
                    raise
                try:
                    self.checkout_target()
                    passed = migration.carry(self.pending, staged)
                    passed.files.require_rehearsal(rehearsal.files)
                    self.publish(backup, selected_route)
                    with ClearMaintenancePhase().running(self.staged.commands.clock):
                        report.owners.release()
                    restarted_services = stopped.start_on(self.staged)
                except BaseException as error:
                    print(f"install: install failed ({type(error).__name__}: {error}); "
                          "restoring original installation", file=sys.stderr)
                    # Until the outgoing runtime opens every restored store, any raise
                    # keeps the hold draining and starts no owner.
                    with report.owners.held_on_failure():
                        # Only the services this run stopped may write the root on its behalf.
                        HostService.stop_all(stopped.services)

                        def rehold() -> None:
                            """Hold the root again in whatever phase it is now in."""
                            holder.close()
                            holder.enter_context(
                                maintenance.held_by(MaintenanceOperator.current()).holding())

                        rehold()
                        backup.undo()
                        rehold()
                        head = self.installed.commands.git(
                            self.checkout, "rev-parse", "HEAD").strip()
                        if head != self.current:
                            with RestoredCheckoutPreparePhase().running(self.staged.commands.clock):
                                self.installed.commands.git(
                                    self.checkout, "reset", "-q", "--keep", self.current)
                                Checkout(self.checkout).prepare()
                        self.installed.publish(self.root)
                        self.installed.require_opens(self.root)
                    holder.close()
                    report.owners.release()
                    print(f"install: restored after failure ({type(error).__name__}: {error}); "
                          "original runtime selected; owners start on it; "
                          f"backup: {backup.description}",
                          file=sys.stderr)
                    raise InstallRolledBack(
                        error, backup.description,
                        runtime=self.installed.runtime) from error
                # Owner startup is reported per owner and never rolls the install back.
                report.writer = RuntimeWriter(self.root, self.staged)
                try:
                    retirement = tuple(replace(
                        step, spill_dir=backup.spill_directory(self.spill_dir),
                    ) for step in self.pending)
                    passed = passed.after_acceptance(retirement, staged)
                finally:
                    started = report.owners.published_on(self.staged, self.root)
                with BackupRetirePhase().running(self.staged.commands.clock):
                    retired_backups = backup.retain(self.spill_dir)
                return passed.extended(
                    CarriedRun, stopped=tuple(report.owners.stopped), services=restarted_services,
                    started=started, restore_space=restore, backup=backup.report(),
                    retired_backups=retired_backups)
            finally:
                holder.close()


class UnchangedInstall(ExistingBusInstall[UnchangedRehearsal]):
    """The checkout already sits at the target and the relocked lock is the installed one:
    nothing to rehearse or carry; its owners start again when the install's hold clears."""

    def rehearsal(self, practice: PracticeBus, reconciled: tuple[Path, ...]) -> UnchangedRehearsal:
        return UnchangedRehearsal()

    def uncopied(self) -> UnchangedRehearsal:
        return UnchangedRehearsal()

    def live(self, rehearsal: UnchangedRehearsal, report: InstallReport) -> UnchangedRun:
        self.installed.publish(self.root)
        # The staged stack is the installed runtime here; its worker is already running.
        report.writer = RuntimeWriter(self.root, self.staged)
        return UnchangedRun()


class FreshInstall(Install[FreshRehearsal]):
    """Nothing was ever written at the root: no carry, rehearsal, hold or owner to restart.
    The target's source creates the bus already in its format and routes to it."""

    @classmethod
    def rehearsed(cls, checkout: Path, root: Path, current: str, target: str,
                  chain: CarryChain, installed: Stack, staged: TargetStack, tree: TargetTree,
                  installer: InstallerChoice, spill_dir: Path) -> tuple[Install, Rehearsal]:
        """Nothing at the root to copy, so nothing is pending."""
        install = cls(checkout, root, current, target, chain, (), installed, staged,
                      tree, installer, spill_dir)
        return install, install.rehearsal()

    @staticmethod
    def initial_writer(root: Path, installed: Stack) -> RootWriter:
        """A fresh bus has nowhere to publish a start message yet."""
        return UnpostedWriter()

    def rehearsal(self) -> FreshRehearsal:
        return FreshRehearsal()

    def live(self, rehearsal: FreshRehearsal, report: InstallReport) -> CreatedRun:
        self.checkout_target()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        wire(self.root).registry.prepare_native_root()
        report.writer = RuntimeWriter(self.root, self.staged)
        backup, selected_route = self.backed_up(())
        with self.staged.services_stopped(self.installed) as stopped:
            try:
                route = self.publish(backup, selected_route)
                services = stopped.start_on(self.staged)
            except BaseException:
                HostService.stop_all(stopped.services)
                backup.restore_route()
                self.installed.publish(self.root)
                raise
            with BackupRetirePhase().running(self.staged.commands.clock):
                backup.retain(self.spill_dir)
            return CreatedRun(services, route)


class RetiringInstall(StopsOwners, FreshInstall):
    """The bus at the root is runtime state the target cannot carry: its owners stop, it
    moves aside intact, and a fresh bus takes its place."""

    def rehearsal(self) -> RetiringRehearsal:
        return RetiringRehearsal()

    def live(self, rehearsal: FreshRehearsal, report: InstallReport) -> RetiredRun:
        retired = self.root.with_name(f"{self.root.name}.retired-{time.strftime('%Y%m%dT%H%M%S')}")
        self.root.rename(retired)
        report.owners.retired()
        return super().live(rehearsal, report).extended(
            RetiredRun, stopped=tuple(report.owners.stopped), retired=retired)
