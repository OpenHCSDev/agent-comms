"""Upgrade a retained bus and the checkout that runs it: what ``agent-comms install`` runs.

``agent-comms install`` checks out the target revision and runs this package from that
revision's ``src`` (``python -m agent_comms.installer``), so its steps are the
target's. The target's stack is built into its own runtime, relocked to the latest
OpenHCSDev mains, and must import Toad and the ACP client. The bus ledger
(``CarryLedger``) names the carry steps still pending; each runs with the target's
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
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, ExitStack
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

from agent_comms.active_route import ActiveRoute, active_route_path, route_new_bus
from agent_comms.cli_bridges.shared import BridgeResume, Serve
from agent_comms.cli_commands import InstallResult, ThreadsResult, WhoResult
from agent_comms.cli_result import CliResult
from agent_comms.comms import Comms
from agent_comms.declared_family import DeclaredFamily
from agent_comms.field_codec import FieldCodec, PathText
from agent_comms.history_result import EverythingHistoryPageResult
from agent_comms.host_services import HostService
from agent_comms.install_choices import InstallerChoice
from agent_comms.installer import publication, step_worker
from agent_comms.installer.backup import (
    BackupReport,
    RestoreSpace,
    SavedFile,
    StoreBackup,
    UpgradeRefused,
    bus_files,
    transcripts_held,
)
from agent_comms.installer.carry import STEPS, Carried, Carry, CarryChain, CarryLedger
from agent_comms.installer.practice import PracticeMeasurement, practice_copy, prepare_runtime_home
from agent_comms.installer.runtime_resolution import RuntimeResolution
from agent_comms.launch_runtime import LaunchRuntime, RestartEnvironment, SpecifiedImportPath
from agent_comms.maintenance_barrier import HOLDER_NONCE, MaintenanceBarrier, MaintenanceOperator
from agent_comms.registry_store import RegistryStore
from agent_comms.repo import Checkout
from agent_comms.root_runtime import RootRuntime, RootRuntimeStore
from agent_comms.runtime_processes import RuntimeProcesses, RuntimeProcessOutcome
from agent_comms.storage_rules import StorageRule
from agent_comms.store_files import _store_lock
from agent_comms.wire_log import WireLog

if TYPE_CHECKING:
    from agent_comms.installer.launch import InstallReport

HOME = Path.home()
CLIENT_IMPORTS = "import toad.app, agent_comms.acp"


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

    @staticmethod
    def declared(steps: tuple[StepRun, ...]) -> frozenset[str]:
        return frozenset({CarryLedger.filename, *(name for step in steps
                                                  for name in step.carried.rewritten)})

    def require_rehearsal(self, rehearsal: RehearsalFiles) -> None:
        outside = sorted(set(self.names) - set(rehearsal.names) - {CarryLedger.filename})
        if outside:
            raise UpgradeRefused(f"Live carries changed files absent from rehearsal: {outside}")


def run(*command: str | Path, cwd: Path = Path(), env: Mapping[str, str] = os.environ) -> str:
    print("+", " ".join(map(str, command)), file=sys.stderr, flush=True)
    completed = subprocess.run([str(part) for part in command], cwd=cwd, env=env,
                               capture_output=True, text=True)
    if completed.returncode:
        raise UpgradeRefused(f"{command[0]} failed: {completed.stderr.strip() or completed.stdout.strip()}")
    return completed.stdout


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
            stack.cli(root, "start", "--name", name)
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
class BusReadBack:
    """What the carried bus answered through the CLI of the runtime that serves it next."""

    threads: int
    who: int
    history: int


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


@dataclass(frozen=True)
class StoppedOwners:
    stopped: tuple[str, ...]

    def stopped_lines(self) -> tuple[str, ...]:
        return (f"Stopped owners: {', '.join(self.stopped) or 'none'}",)


@dataclass(frozen=True)
class RestartedServices:
    services: tuple[str, ...]

    def service_lines(self) -> tuple[str, ...]:
        return (f"Restarted services: {', '.join(self.services) or 'none'}",)


class Rehearsal(DeclaredFamily, affix="Rehearsal"):
    """What a run tried before it touched the live bus."""


@dataclass(frozen=True)
class CarriedRehearsal(CarriedPass, Rehearsal):
    copy: PracticeMeasurement


@dataclass(frozen=True)
class UnchangedRehearsal(Rehearsal):
    """Nothing to rehearse: the checkout and lock are already the target's."""


@dataclass(frozen=True)
class FreshRehearsal(Rehearsal):
    """Nothing to rehearse: the target creates the bus."""


@dataclass(frozen=True)
class RetiringRehearsal(FreshRehearsal):
    """The bus at the root moves aside before a fresh one is created there."""


@dataclass(frozen=True, kw_only=True)
class LiveRun(DeclaredFamily, affix="Run"):
    """What a live install changed, including every bridge and old-runtime process."""

    processes: tuple[RuntimeProcessOutcome, ...] = ()
    bridges: tuple[BridgeResume, ...] = ()

    def lines(self) -> tuple[str, ...]:
        return (
            *self.change_lines(),
            *(line for bridge in self.bridges for line in bridge.lines()),
            *(process.line() for process in self.processes),
        )

    @abstractmethod
    def change_lines(self) -> tuple[str, ...]: ...


@dataclass(frozen=True)
class CarriedRun(CarriedPass, StoppedOwners, RestartedServices, LiveRun):
    started: tuple[OwnerStart, ...]
    restore_space: RestoreSpace
    backup: BackupReport
    retired_backups: tuple[Annotated[Path, PathText], ...]

    def change_lines(self) -> tuple[str, ...]:
        return (
            *(f"Carried {step.carry}: {step.carried.changed} changed" for step in self.steps),
            *self.stopped_lines(),
            *(line for owner in self.started for line in owner.lines()),
            *self.service_lines(),
            f"Backup: {self.backup.backup} ({self.backup.bytes} bytes)",
        )


@dataclass(frozen=True)
class UnchangedRun(LiveRun):
    """Published the installed runtime again and planned the next install."""

    def change_lines(self) -> tuple[str, ...]:
        return ("Runtime republished; nothing carried",)


@dataclass(frozen=True)
class CreatedRun(RestartedServices, LiveRun, Extendable):
    route: str

    def change_lines(self) -> tuple[str, ...]:
        return (f"Created bus; route: {self.route}", *self.service_lines())


@dataclass(frozen=True)
class RetiredRun(CreatedRun, StoppedOwners):
    retired: Annotated[Path, PathText]

    def change_lines(self) -> tuple[str, ...]:
        return (*super().change_lines(), *self.stopped_lines(), f"Retired bus: {self.retired}")


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


@dataclass(frozen=True)
class InstalledUpgrade(RehearsedUpgrade):
    live: LiveRun
    install: InstallResult
    """The harnesses registered on the bus the target now reads."""

    def lines(self) -> tuple[str, ...]:
        return (*super().lines(), *self.live.lines())


@dataclass(frozen=True)
class Stack:
    """One built installation and the source checkout containing its install tools."""

    checkout: Path
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
        return self.home / ".local/state/agent-comms/upgrade-bus/uv"

    def require_uv(self) -> None:
        if not os.access(self.uv, os.X_OK):
            raise UpgradeRefused(f"No uv executable at {self.uv}; the installer runs only that uv")

    @classmethod
    def build(cls, checkout: Path, home: Path = HOME) -> Stack:
        """Resolve current mains once, then build or reuse their immutable installation."""
        source = cls(checkout, home)
        source.require_uv()
        run(source.uv, "lock", "--project", "stack", "--upgrade", "--python", "3.14",
            cwd=checkout)
        core = run("git", "-C", checkout, "rev-parse", "HEAD").strip()
        resolution = hashlib.sha256(source.lock.read_bytes()).hexdigest()[:16]
        prefix = home / ".local/share/agent-comms/runtimes" / f"core-{core[:9]}-lock-{resolution}"
        built = replace(source, runtime=LaunchRuntime(prefix / "bin/python"))
        if prefix.exists():
            if not RuntimeResolution.at(prefix).matches(source.lock, built.python, core):
                raise UpgradeRefused("Existing built runtime differs from its resolved inputs")
            return built
        prefix.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        prefix.mkdir(mode=0o700)
        try:
            run(source.uv, "sync", "--project", "stack", "--locked", "--python", "3.14",
                "--no-editable", cwd=checkout,
                env={**built.environment(), "UV_PROJECT_ENVIRONMENT": str(prefix)})
            run(checkout / "stack/bin/prepare-pi-native", "--python", built.python,
                cwd=checkout, env=built.environment(checkout / "src"))
            native = Path(run(built.python, "-c",
                "from agent_comms.native_package import pinned_location; print(pinned_location())",
                cwd=checkout, env=built.environment(checkout / "src")).strip())
            package = Path(run(built.python, "-c",
                "import agent_comms; from pathlib import Path; "
                "print(Path(agent_comms.__file__).parent)",
                cwd=checkout, env=built.environment()).strip())
            destination = package / "_native" / native.relative_to(checkout / "stack")
            destination.parents[2].mkdir(mode=0o700, parents=True)
            shutil.copytree(native, destination)
            RuntimeResolution.capture(built.python, core).save(prefix)
            built.imports()
            built.prepare_native()
        except BaseException:
            shutil.rmtree(prefix)
            raise
        return built

    def imports(self) -> None:
        run(self.python, "-c", CLIENT_IMPORTS, cwd=self.checkout, env=self.environment())

    def prepare_native(self) -> None:
        run(self.python, "-c", "from agent_comms.native_package import "
            "VerifiedNativePackage,pinned_native_package; "
            "from agent_comms.unit_instructions import UnitInstructions; "
            "VerifiedNativePackage.acquire(pinned_native_package()); UnitInstructions.prepare()",
            cwd=self.checkout, env=self.environment())

    def publish(self) -> None:
        """The selected interpreter owns console metadata and harness declarations."""
        run(
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

    def cli(self, root: Path, *arguments: str) -> dict:
        """One JSON CLI result for this bus alone, never the route's, waiting while the
        route index is still being built."""
        for _ in range(240):
            completed = subprocess.run(
                [str(self.python), "-c", "from agent_comms.cli import main; main()", "--root", str(root),
                 *arguments], cwd=self.checkout, env=self.environment(), capture_output=True,
                text=True)
            if "Route index covers" not in completed.stdout:
                break
            time.sleep(5)
        result = json.loads(completed.stdout or "{}")
        if completed.returncode or "error" in result:
            raise UpgradeRefused(f"agent-comms {' '.join(arguments)}: "
                                 f"{completed.stdout.strip() or completed.stderr.strip()}")
        return result

    def call_step(self, step: type[Carry], root: Path, tree: TargetTree,
                  method: str, **arguments: bool) -> str:
        """Run the step in the target tree with this stack's interpreter."""
        output = run(self.python, "-m", step_worker.__name__, step.name(), method, root,
                     json.dumps(arguments), cwd=tree.path,
                     env=self.environment(*tree.source_paths()))
        return output.strip().splitlines()[-1]

    def backup_files(self, root: Path, pending: tuple[type[Carry], ...],
                     tree: TargetTree) -> tuple[Path, ...]:
        return tuple(sorted({
            path for step in pending for path in FieldCodec.decode(
                tuple[Annotated[Path, PathText], ...], FieldCodec.parse(self.call_step(
                    step, root, tree, Carry.backup_files.__name__)))
        }))

    def running_owners(self, root: Path) -> list[str]:
        """Use the restart request's receiving declaration in the selected runtime."""
        selected = run(self.python, "-c", """import json, sys
from pathlib import Path
from agent_comms.comms import Comms
from agent_comms.owner_restart import OwnerRestartRequest
snapshot = Comms(Path(sys.argv[1])).registry.snapshot()
print(json.dumps([thread.name for thread in OwnerRestartRequest().scope.threads(snapshot)]))
""", root, cwd=self.checkout, env=self.environment())
        return FieldCodec.decode(list[str], FieldCodec.parse(selected))

    def owners(self, root: Path, command: str, names: list[str]) -> None:
        """Run the captured owner set together and join every request before proceeding."""
        with ThreadPoolExecutor(max_workers=max(1, len(names))) as pool:
            list(pool.map(lambda name: self.cli(root, command, "--name", name), names))

    def start(self, root: Path, names: list[str]) -> tuple[OwnerStart, ...]:
        """Start the captured owner set together; each owner's outcome is its own."""
        with ThreadPoolExecutor(max_workers=max(1, len(names))) as pool:
            return tuple(pool.map(lambda name: OwnerStart.attempt(self, root, name), names))

    def listed(self, root: Path, result: type[CliResult], *arguments: str) -> int:
        """How many items a listing answered, under its result's one field."""
        (listing,) = fields(result)
        return len(self.cli(root, *arguments)[listing.name])

    def verify(self, root: Path) -> BusReadBack:
        history = self.cli(root, "history", "--limit", "50")  # #any: every channel
        return BusReadBack(self.listed(root, ThreadsResult, "threads"),
                           self.listed(root, WhoResult, "who"),
                           len(history[EverythingHistoryPageResult.messages_key]))


@dataclass(frozen=True)
class TargetTree(AbstractContextManager["TargetTree"]):
    """The temporary worktree and scratch lifetime owned by one install."""

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
        Checkout.add(self.repo, self.path, self.revision, "--detach", "-q")

    def close(self) -> None:
        try:
            subprocess.run(["git", "-C", str(self.repo), "worktree", "remove", "--force", str(self.path)])
        finally:
            shutil.rmtree(self.scratch, ignore_errors=True)

    def __exit__(self, *_error: object) -> None:
        self.close()


def carry(step: type[Carry], root: Path, stack: Stack, tree: TargetTree, *flags: str) -> Carried:
    """Run one step with the target's src and ``stack``'s Python: the target's staged
    stack, never the installed one."""
    operation = Carry.before_stop.__name__ if "--before-stop" in flags else Carry.carry.__name__
    output = stack.call_step(step, root, tree, operation, apply=True)
    return FieldCodec.decode(Carried, FieldCodec.parse(output))


def before_stop(
    root: Path, pending: tuple[type[Carry], ...], stack: Stack, tree: TargetTree
) -> tuple[StepRun, ...]:
    """What the running installation needs before its own stop command reads the bus."""
    with _store_lock(root / "wire"):
        return tuple(StepRun(step.name(), carry(step, root, stack, tree, "--before-stop", "--apply"))
                     for step in pending)


def carry_all(
    root: Path, pending: tuple[type[Carry], ...], stack: Stack, tree: TargetTree
) -> tuple[StepRun, ...]:
    results = []
    for step in pending:
        with _store_lock(root / "wire"):
            results.append(StepRun(step.name(), CarryLedger(root).apply(
                step.name(), lambda: carry(step, root, stack, tree, "--apply"))))
    return tuple(results)


def rehearse(root: Path, stack: Stack, installed: Stack, tree: TargetTree, chain: CarryChain,
             installer: InstallerChoice) -> CarriedRehearsal:
    """The live run on a practice copy, up to stopping owners: the outgoing runtime answers
    the same owner probe the live run asks it before the stop."""
    parent = installer.scratch(StorageRule.load(root))
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (
        tempfile.TemporaryDirectory(dir=parent, prefix="practice-") as scratch,
        practice_copy(root, Path(scratch) / "bus") as (copied, measurement),
    ):
        home = Path(scratch) / "home"
        prepare_runtime_home(home)
        (home / ".claude").symlink_to(Path.home() / ".claude")
        stack = replace(stack, home=home)
        stack.prepare_native()
        pending = chain.pending(CarryLedger(copied).applied())
        for path in stack.backup_files(root, pending, tree):
            destination = copied / path.relative_to(root)
            if path.exists() and not destination.exists():
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, destination)
        installed.running_owners(copied)
        maintenance = MaintenanceBarrier(RegistryStore.at(copied).path)
        held = maintenance.begin(MaintenanceOperator.current())
        held = maintenance.retarget(held, stack.runtime)
        os.environ[HOLDER_NONCE] = held.nonce
        with transcripts_held(copied):
            before = bus_files(copied)
        stopping = before_stop(copied, pending, stack, tree)
        passed = carry_and_read(copied, before, pending, stack, stack, tree, stopping)
        return passed.extended(CarriedRehearsal, copy=measurement)


def carry_and_read(root: Path, before: dict[str, SavedFile], pending: tuple[type[Carry], ...],
                   staged: Stack, reader: Stack, tree: TargetTree,
                   stopping: tuple[StepRun, ...]) -> CarriedPass:
    """The pass a rehearsal and a live install share once ``before`` was read under the
    transcript hold. The hold is released first: SQLite cannot lend its writer lock to a
    step's process, so a step that writes the transcript store could not run under it."""
    steps = (*stopping, *carry_all(root, pending, staged, tree))
    files = carried(root, before, steps, RehearsalFiles.declared(steps))
    return CarriedPass(steps, files, reader.verify(root))


def upgrade(checkout: Path, root: Path, target: str, installer: InstallerChoice, rehearse_only: bool,
            retire: bool, spill_dir: Path, scratch: Path,
            harnesses: Callable[[], InstallResult], report: InstallReport) -> Upgrade:
    """``scratch`` holds the target's worktree the launcher runs this source from; this
    run owns it and removes it. A live run ends by registering the ``harnesses``."""
    tree = TargetTree(checkout, scratch, target)
    with report.running(tree):
        current = run("git", "-C", checkout, "rev-parse", "HEAD").strip()
        if subprocess.run(["git", "-C", str(checkout), "merge-base", "--is-ancestor", current,
                           target]).returncode:
            raise UpgradeRefused(f"{target[:12]} does not follow the checkout's {current[:12]}")
        if run("git", "-C", checkout, "status", "--porcelain", "--untracked-files=no").strip():
            raise UpgradeRefused("The checkout has local changes")
        chain = CarryChain.at(tree.path)
        member = Install.member(root, retire)
        try:
            pending = member.pending_steps(root, chain)
        except ValueError as error:
            raise UpgradeRefused(str(error)) from None
        installed = Stack(checkout)
        installed.require_uv()
        if not installed.python.exists():
            raise UpgradeRefused(f"No installed stack at {installed.python}")
        staged = Stack.build(tree.path)
        install = member.of(checkout, root, current, target, chain, pending, installed, staged,
                            tree, installer, spill_dir)
        rehearsal = install.rehearsal()
        names = tuple(step.name() for step in pending)
        if rehearse_only:
            return report.complete(RehearsedUpgrade(current, target, root, names, rehearsal))
        result = InstalledUpgrade(current, target, root, names, rehearsal,
                                  install.install(rehearsal), harnesses())
        return report.complete(result)


@dataclass(frozen=True)
class Install[Rehearsed: Rehearsal](ABC):
    """One run once the target's stack is relocked: what it changes on the bus. Its live
    pass takes the rehearsal its own kind produced."""

    checkout: Path
    root: Path
    current: str
    target: str
    chain: CarryChain
    pending: tuple[type[Carry], ...]
    installed: Stack
    staged: Stack
    tree: TargetTree
    installer: InstallerChoice
    spill_dir: Path

    @staticmethod
    def member(root: Path, retire: bool) -> type[Install]:
        """What exists at ``root`` decides how much of an install it needs."""
        if retire:
            return RetiringInstall
        if WireLog(WireLog.path_in(root)).fresh():
            return FreshInstall
        return Install

    @classmethod
    def pending_steps(cls, root: Path, chain: CarryChain) -> tuple[type[Carry], ...]:
        return chain.pending(CarryLedger(root).applied())

    @classmethod
    def of(cls, checkout: Path, root: Path, current: str, target: str, chain: CarryChain,
           pending: tuple[type[Carry], ...], installed: Stack, staged: Stack, tree: TargetTree,
           installer: InstallerChoice, spill_dir: Path) -> Install:
        """A bus already carried, on the target, built from the installed lock needs nothing."""
        unchanged = not pending and current == target and RuntimeResolution.installed().matches(
            staged.lock, staged.python, target)
        member = (ChangedInstall, UnchangedInstall)[unchanged]
        return member(checkout, root, current, target, chain, pending, installed, staged,
                      tree, installer, spill_dir)

    @abstractmethod
    def rehearsal(self) -> Rehearsed: ...

    def install(self, rehearsal: Rehearsed) -> LiveRun:
        """Session bridges close their bus resources before units stop, then exec in place.

        Each serve retains its recorded owner's PID and start time after the hook exits.
        Its control connection survives until live() publishes or rolls back; the
        installed pointer, not a copied target version, selects its successor.
        """
        with ExitStack() as resources:
            paused = tuple(bridge.pause(resources)
                           for bridge in Serve.running(self.installed.home))
            try:
                result = self.live(rehearsal)
            finally:
                # A failed live pass has restored the outgoing runtime; resume onto it too.
                resumed = tuple(bridge.resume() for bridge in paused)
        # Observe after bridge transfer. Discovery never grants stop authority
        # over another app; installer-started children keep their launch handles.
        processes = RuntimeProcesses.observe(self.staged.runtime).outcomes
        return replace(result, bridges=resumed, processes=processes)

    @abstractmethod
    def live(self, rehearsal: Rehearsed) -> LiveRun: ...


class ChangedInstall(Install[CarriedRehearsal]):
    def rehearsal(self) -> CarriedRehearsal:
        return rehearse(self.root, self.staged, self.installed, self.tree, self.chain,
                        self.installer)

    def live(self, rehearsal: CarriedRehearsal) -> CarriedRun:
        return live(self.checkout, self.root, self.current, self.target, self.pending,
                    self.installed, self.staged, self.tree, self.installer, self.spill_dir,
                    rehearsal.files)


class UnchangedInstall(Install[UnchangedRehearsal]):
    """The checkout already sits at the target and the relocked lock is the installed one:
    nothing to rehearse, hold or restart."""

    def rehearsal(self) -> UnchangedRehearsal:
        return UnchangedRehearsal()

    def live(self, rehearsal: UnchangedRehearsal) -> UnchangedRun:
        self.installed.publish()
        RootRuntime.publish(self.root, self.installed.runtime)
        return UnchangedRun()


class FreshInstall(Install[FreshRehearsal]):
    """Nothing was ever written at the root: no carry, rehearsal, hold or owner to restart.
    The target's source creates the bus already in its format and routes to it."""

    @classmethod
    def pending_steps(cls, root: Path, chain: CarryChain) -> tuple[type[Carry], ...]:
        return ()

    @classmethod
    def of(cls, checkout: Path, root: Path, current: str, target: str, chain: CarryChain,
           pending: tuple[type[Carry], ...], installed: Stack, staged: Stack, tree: TargetTree,
           installer: InstallerChoice, spill_dir: Path) -> Install:
        return cls(checkout, root, current, target, chain, pending, installed, staged,
                   tree, installer, spill_dir)

    def rehearsal(self) -> FreshRehearsal:
        return FreshRehearsal()

    def live(self, rehearsal: FreshRehearsal) -> CreatedRun:
        run("git", "-C", self.checkout, "merge", "--ff-only", "-q", self.target)
        Checkout(self.checkout).prepare()
        self.staged.prepare_native()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        root_id = Comms(self.root).messaging.initialize_private_initial_protocol()
        CarryLedger(self.root).start(self.chain)
        self.staged.publish()
        RootRuntime.publish(self.root, self.staged.runtime)
        route = route_new_bus(ActiveRoute(self.root.absolute(), root_id), active_route_path())
        services = HostService.restart_running(self.staged.python)
        return CreatedRun(services, route)


class RetiringInstall(FreshInstall):
    """The bus at the root is runtime state the target cannot carry: its owners stop, it
    moves aside intact, and a fresh bus takes its place."""

    def rehearsal(self) -> RetiringRehearsal:
        return RetiringRehearsal()

    def live(self, rehearsal: FreshRehearsal) -> RetiredRun:
        running = self.installed.running_owners(self.root)
        self.installed.owners(self.root, "stop", running)
        retired = self.root.with_name(f"{self.root.name}.retired-{time.strftime('%Y%m%dT%H%M%S')}")
        self.root.rename(retired)
        return super().live(rehearsal).extended(RetiredRun, stopped=tuple(running), retired=retired)


def live(checkout: Path, root: Path, current: str, target: str, pending: tuple[type[Carry], ...],
         installed: Stack, staged: Stack, tree: TargetTree, installer: InstallerChoice,
         spill_dir: Path, rehearsal_files: RehearsalFiles) -> CarriedRun:
    """The rehearsal's passes under the rehearsal's stack; ``installed`` only stops, starts
    and reads back its own owners."""
    expected = frozenset((*rehearsal_files.names, CarryLedger.filename)) | frozenset(
        str(path.relative_to(root)) for path in staged.backup_files(root, pending, tree))
    selected = tuple(root / name for name in sorted(expected)) if pending else ()
    restore = RestoreSpace.measure(max(
        rehearsal_files.restore_bytes,
        sum(path.stat().st_size for path in selected if path.exists()),
    ), root.parent)
    restore.require()
    RootRuntimeStore.at(root).owner(installed.runtime)
    running = installed.running_owners(root)
    maintenance = MaintenanceBarrier(RegistryStore.at(root).path)
    held = maintenance.begin(MaintenanceOperator.current())
    os.environ[HOLDER_NONCE] = held.nonce  # this run's own CLI calls read under its hold
    try:
        installed.owners(root, "stop", running)
        with transcripts_held(root):
            backup = StoreBackup.take(root, active_route_path(), selected, spill_dir)
    except BaseException as error:
        maintenance.clear(held)
        started = installed.start(root, running)
        print(f"install: aborted before the backup ({type(error).__name__}: {error}); "
              f"maintenance cleared; owners: {started}", file=sys.stderr)
        raise
    try:
        # The outgoing CLI has finished. No outgoing reader sees this target-bearing hold.
        held = maintenance.retarget(held, staged.runtime)
        os.environ[HOLDER_NONCE] = held.nonce
        with transcripts_held(root):
            before = bus_files(root)
        stopping = before_stop(root, pending, staged, tree)
        run("git", "-C", checkout, "merge", "--ff-only", "-q", target)
        Checkout(checkout).prepare()
        staged.prepare_native()
        passed = carry_and_read(root, before, pending, staged, staged, tree, stopping)
        passed.files.require_rehearsal(rehearsal_files)
        staged.publish()
        RootRuntime.publish(root, staged.runtime)
        maintenance.clear(held)
        services = HostService.restart_running(staged.python)
    except BaseException as error:
        print(f"install: install failed ({type(error).__name__}: {error}); "
              "restoring original installation", file=sys.stderr)
        state = maintenance.read()
        recovery = state.phase.hold_for(maintenance, state, MaintenanceOperator.current())
        os.environ[HOLDER_NONCE] = recovery.nonce
        if not backup.original():
            backup.restore()
        restored = maintenance.read()
        restored_hold = restored.phase.hold_for(maintenance, restored, MaintenanceOperator.current())
        os.environ[HOLDER_NONCE] = restored_hold.nonce
        if run("git", "-C", checkout, "rev-parse", "HEAD").strip() != current:
            run("git", "-C", checkout, "reset", "-q", "--keep", current)
            Checkout(checkout).prepare()
        # Republish only when current moved: the outgoing runtime may predate this publisher.
        if LaunchRuntime.installed().prefix.resolve() != installed.runtime.prefix.resolve():
            installed.publish()
        RootRuntime.publish(root, installed.runtime)
        maintenance.clear(restored_hold)
        started = installed.start(root, running)
        print(f"install: restored after failure ({type(error).__name__}: {error}); "
              f"original runtime selected; owners: {started}; "
              f"backup: {backup.description}",
              file=sys.stderr)
        raise InstallRolledBack(error, started, backup.description,
                                runtime=installed.runtime) from error
    # Installed: the carries ran and the runtime is published. Starting the owners it
    # stopped is reported per owner and never rolls the install back.
    started = staged.start(root, running)
    retired_backups = backup.retain(spill_dir)
    return passed.extended(CarriedRun, stopped=tuple(running), services=services, started=started,
                           restore_space=restore, backup=backup.report(),
                           retired_backups=retired_backups)
