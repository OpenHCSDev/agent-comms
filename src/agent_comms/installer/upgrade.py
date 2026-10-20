"""Upgrade a retained bus and the checkout that runs it: what ``agent-comms install`` runs.

``agent-comms install`` checks out the target revision and runs this package from that
revision's ``src`` (``python -m agent_comms.installer``), so its steps are the
target's. The target's stack is built into its own runtime, relocked to the latest
OpenHCSDev mains, and must import Toad and the ACP client. The bus ledger
(``carries.json``) names the carry steps still pending; each runs from the revision
that declares it (``agent_comms.installer.carry``). Every run first rehearses on a
copy of the bus: the steps, then ``who``, ``threads`` and ``history`` reads, and
every file no step names as rewritten must be unchanged by saved-file size,
nanosecond mtime and inode. ``--rehearse`` stops there.

A live run then stops the running agent owners with the installed CLI, backs up only
files the rehearsal changed, moves the checkout, prepares native Pi, carries the bus,
reads it back, publishes the built runtime and starts only the owners it stopped. A
failure after the backup restores the original runtime and data. Nothing is sent and
no input is replayed: owners start by the normal start path.
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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Annotated

import agent_comms.activity_checkpoint  # noqa: F401  (declares the activity runtime files)
from agent_comms.active_route import ActiveRoute, route_new_bus
from agent_comms.comms import Comms
from agent_comms.field_codec import FieldCodec, PathText
from agent_comms.history_result import EverythingHistoryPageResult
from agent_comms.storage_rules import StorageRule
from agent_comms.host_services import HostService
from agent_comms.launch_runtime import LaunchRuntime, RestartEnvironment, SpecifiedImportPath
from agent_comms.maintenance_barrier import HOLDER_NONCE, MaintenanceBarrier, MaintenanceOperator
from agent_comms.registry_store import RegistryStore
from agent_comms.store_files import _store_lock
from agent_comms.wire_log import WireLog
from agent_comms.installer import publication, step_worker
from agent_comms.installer.backup import (
    NoBackup,
    RestoreSpace,
    SavedFile,
    StoreBackup,
    UpgradeRefused,
    bus_files,
    transcripts_held,
)
from agent_comms.installer.carry import Carried, Carry, CarryChain, CarryLedger, Step, TodoColumnCarry
from agent_comms.installer.practice import practice_copy, prepare_runtime_home
from agent_comms.installer.runtime_resolution import RuntimeResolution
from agent_comms.todos import PlannedInstallDeadline

HOME = Path.home()
CLIENT_IMPORTS = "import toad.app, agent_comms.acp"
ROUTE = Path.home() / ".local/state/agent-comms/active-route.json"


@dataclass(frozen=True)
class RehearsalFiles:
    """The rehearsal's named rewrite set and the restore reservation it requires."""

    identical: int
    rewritten: int
    names: tuple[str, ...]
    restore_bytes: int

    @staticmethod
    def declared(steps: list[dict]) -> frozenset[str]:
        return frozenset({CarryLedger.name, *(name for step in steps
                                              for name in step["rewritten"])})

    def require_rehearsal(self, rehearsal: RehearsalFiles) -> None:
        outside = sorted(set(self.names) - set(rehearsal.names) - {CarryLedger.name})
        if outside:
            raise UpgradeRefused(f"Live carries changed files absent from rehearsal: {outside}")


def run(*command: str | Path, cwd: Path | None = None, env: dict | None = None) -> str:
    print("+", " ".join(map(str, command)), file=sys.stderr, flush=True)
    completed = subprocess.run([str(part) for part in command], cwd=cwd, env=env,
                               capture_output=True, text=True)
    if completed.returncode:
        raise UpgradeRefused(f"{command[0]} failed: {completed.stderr.strip() or completed.stdout.strip()}")
    return completed.stdout


def carried(root: Path, before: dict[str, SavedFile], steps: list[dict],
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
            "VerifiedNativePackage.acquire(pinned_native_package())",
            cwd=self.checkout, env=self.environment())

    def publish(self) -> None:
        """The selected interpreter owns console metadata and harness declarations."""
        run(
            self.python, "-m", publication.__name__,
            cwd=self.checkout, env=self.environment(),
        )

    def environment(self, *paths: Path) -> dict:
        """Use the artifact's packages; only historical carry helpers select explicit source."""
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

    def call_step(self, step: Step, root: Path, revisions: Revisions,
                  method: str, **arguments: bool) -> str:
        """Run the declaration in its family's source with this stack's interpreter."""
        tree = revisions.tree(step.family.source_revision(step))
        output = run(self.python, "-m", "cutover.step_worker", f"cutover.{step.name}",
                     step.declaration, method, root, json.dumps(arguments), cwd=tree,
                     env=self.environment(tree / "src", tree / "tools"))
        return output.strip().splitlines()[-1]

    def backup_files(self, root: Path, pending: tuple[Step, ...],
                     revisions: Revisions) -> tuple[Path, ...]:
        return tuple(sorted({
            path for step in pending for path in FieldCodec.decode(
                tuple[Annotated[Path, PathText], ...], FieldCodec.parse(self.call_step(
                    step, root, revisions,
                    Carry.backup_files.__name__)))
        }))

    def running_owners(self, root: Path) -> list[str]:
        """Use the restart request's receiving declaration in the selected runtime."""
        selected = run(self.python, "-c", """import json, sys
from pathlib import Path
from agent_comms.comms import Comms
from agent_comms.owner_restart import OwnerRestartRequest
snapshot = Comms(Path(sys.argv[1])).registry.snapshot()
print(json.dumps([thread.name for thread in OwnerRestartRequest().threads(snapshot)]))
""", root, cwd=self.checkout, env=self.environment())
        return FieldCodec.decode(list[str], FieldCodec.parse(selected))

    def owners(self, root: Path, command: str, names: list[str]) -> None:
        """Run the captured owner set together and join every request before proceeding."""
        with ThreadPoolExecutor(max_workers=max(1, len(names))) as pool:
            list(pool.map(lambda name: self.cli(root, command, "--name", name), names))

    def verify(self, root: Path) -> dict[str, int]:
        threads = self.cli(root, "threads")["threads"]
        present = self.cli(root, "who")
        history = self.cli(root, "history", "--limit", "50")  # #any: every channel
        return {"threads": len(threads), "who": len(present["who"]),
                "history": len(history[EverythingHistoryPageResult.messages_key])}


@dataclass
class Revisions:
    """Temporary worktrees of the revisions the pending steps come from."""

    repo: Path
    scratch: Path

    def __post_init__(self) -> None:
        self.trees: dict[str, Path] = {}

    @staticmethod
    def prepare(path: Path) -> None:
        run("git", "-C", path, "submodule", "update", "--init", "--recursive", "--checkout")

    def path(self, revision: str) -> Path:
        return self.scratch / f"tree-{revision[:12]}"

    def add(self, revision: str) -> Path:
        """Check ``revision`` out; the launcher adds the target's tree before running it."""
        path = self.path(revision)
        run("git", "-C", self.repo, "worktree", "add", "--detach", "-q", path, revision)
        return path

    def tree(self, revision: str) -> Path:
        if revision not in self.trees:
            self.trees[revision] = self.staged(self.add(revision))
        return self.trees[revision]

    def adopt(self, revision: str) -> None:
        """Own the tree the launcher added, so this run stages and removes it."""
        self.trees[revision] = self.staged(self.path(revision))

    def staged(self, path: Path) -> Path:
        self.prepare(path)
        # A historical regular cutover package cannot discover a worker from a
        # second PYTHONPATH entry. Stage this transport helper, not new Core.
        shutil.copy2(step_worker.__file__, path / "tools" / "cutover" / "step_worker.py")
        return path

    def close(self) -> None:
        for path in self.trees.values():
            subprocess.run(["git", "-C", str(self.repo), "worktree", "remove", "--force", str(path)])


def carry(step: Step, root: Path, stack: Stack, revisions: Revisions, *flags: str) -> Carried:
    """Run one step with the src of the revision its family names, and
    ``stack``'s Python: the target's staged stack, never the installed one."""
    if step.columns:
        if "--before-stop" in flags:
            return TodoColumnCarry.before_stop(root, apply=True)
        return TodoColumnCarry.carry_step(
            root, step, CarryChain.at(revisions.repo, step.revision), apply=True)
    operation = Carry.before_stop.__name__ if "--before-stop" in flags else Carry.carry.__name__
    output = stack.call_step(step, root, revisions, operation, apply=True)
    return FieldCodec.decode(Carried, FieldCodec.parse(output))


def before_stop(
    root: Path, pending: tuple[Step, ...], stack: Stack, revisions: Revisions
) -> list[dict]:
    """What the running installation needs before its own stop command reads the bus."""
    with _store_lock(root / "wire"):
        return [carry(step, root, stack, revisions, "--before-stop", "--apply").report(
            step.name, True) for step in pending]


def carry_all(
    root: Path, pending: tuple[Step, ...], stack: Stack, revisions: Revisions
) -> list[dict]:
    results = []
    for step in pending:
        with _store_lock(root / "wire"):
            result = CarryLedger(root).apply(
                CarryChain.at(revisions.repo, step.revision), step.name,
                lambda: carry(step, root, stack, revisions, "--apply"))
        results.append(result.report(step.name, True))
    return results


def rehearse(root: Path, stack: Stack, revisions: Revisions, installer: str) -> dict:
    parent = StorageRule.load(root).initial_roots(installer).scratch
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
        target = run("git", "-C", stack.checkout, "rev-parse", "HEAD").strip()
        pending = CarryChain.at(revisions.repo, target).pending(CarryLedger(copied).applied())
        for path in stack.backup_files(root, pending, revisions):
            destination = copied / path.relative_to(root)
            if path.exists() and not destination.exists():
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, destination)
        stopping = before_stop(copied, pending, stack, revisions)
        os.environ[HOLDER_NONCE] = MaintenanceBarrier(copied / "registry.json").begin(
            MaintenanceOperator.current()).nonce
        with transcripts_held(copied):
            before = bus_files(copied)
        report = carry_and_read(copied, before, pending, stack, stack, revisions, stopping)
        return {**report, "copy": asdict(measurement)}


def carry_and_read(root: Path, before: dict[str, SavedFile], pending: tuple[Step, ...],
                   staged: Stack, reader: Stack, revisions: Revisions,
                   stopping: list[dict]) -> dict:
    """The pass a rehearsal and a live install share once ``before`` was read under the
    transcript hold. The hold is released first: SQLite cannot lend its writer lock to a
    step's process, so a step that writes the transcript store could not run under it."""
    steps = [*stopping, *carry_all(root, pending, staged, revisions)]
    files = carried(root, before, steps, RehearsalFiles.declared(steps))
    return {"steps": steps, "files": files, "read": reader.verify(root)}


def upgrade(checkout: Path, root: Path, target: str, installer: str, rehearse_only: bool,
            retire: bool, spill_dir: Path, scratch: Path) -> dict:
    """``scratch`` holds the target's worktree the launcher runs this source from; this
    run owns it and removes it with every other revision tree it adds."""
    current = run("git", "-C", checkout, "rev-parse", "HEAD").strip()
    revisions = Revisions(checkout, scratch)
    try:
        revisions.adopt(target)
        if subprocess.run(["git", "-C", str(checkout), "merge-base", "--is-ancestor", current,
                           target]).returncode:
            raise UpgradeRefused(f"{target[:12]} does not follow the checkout's {current[:12]}")
        if run("git", "-C", checkout, "status", "--porcelain", "--untracked-files=no").strip():
            raise UpgradeRefused("The checkout has local changes")
        chain = CarryChain.at(checkout, target)
        member = Install.member(root, retire)
        try:
            pending = member.pending_steps(root, chain)
        except ValueError as error:
            raise UpgradeRefused(str(error)) from None
        installed = Stack(checkout)
        installed.require_uv()
        if not rehearse_only and not installed.python.exists():
            raise UpgradeRefused(f"No installed stack at {installed.python}")
        staged = Stack.build(revisions.tree(target))
        install = member.of(checkout, root, current, target, chain, pending, installed, staged,
                            revisions, installer, spill_dir)
        rehearsal = install.rehearsal()
        report = {"from": current, "to": target, "pending": [step.name for step in pending],
                  "rehearsal": rehearsal}
        if rehearse_only:
            return report
        report["live"] = install.live(rehearsal)
        return report
    finally:
        revisions.close()
        shutil.rmtree(scratch, ignore_errors=True)


@dataclass(frozen=True)
class Install(ABC):
    """One run once the target's stack is relocked: what it changes on the bus."""

    checkout: Path
    root: Path
    current: str
    target: str
    chain: CarryChain
    pending: tuple[Step, ...]
    installed: Stack
    staged: Stack
    revisions: Revisions
    installer: str
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
    def pending_steps(cls, root: Path, chain: CarryChain) -> tuple[Step, ...]:
        return chain.pending(CarryLedger(root).applied())

    @classmethod
    def of(cls, checkout: Path, root: Path, current: str, target: str, chain: CarryChain,
           pending: tuple[Step, ...], installed: Stack, staged: Stack, revisions: Revisions,
           installer: str, spill_dir: Path) -> Install:
        """A bus already carried, on the target, built from the installed lock needs nothing."""
        unchanged = not pending and current == target and RuntimeResolution.installed().matches(
            staged.lock, staged.python, target)
        member = (ChangedInstall, UnchangedInstall)[unchanged]
        return member(checkout, root, current, target, chain, pending, installed, staged,
                      revisions, installer, spill_dir)

    @abstractmethod
    def rehearsal(self) -> dict: ...

    @abstractmethod
    def live(self, rehearsal: dict) -> dict: ...


class ChangedInstall(Install):
    def rehearsal(self) -> dict:
        return rehearse(self.root, self.staged, self.revisions, self.installer)

    def live(self, rehearsal: dict) -> dict:
        return live(self.checkout, self.root, self.current, self.target, self.pending,
                    self.installed, self.staged, self.revisions, self.installer, self.spill_dir,
                    rehearsal['files'])


class UnchangedInstall(Install):
    """The checkout already sits at the target and the relocked lock is the installed one:
    nothing to rehearse, hold or restart. It only plans the next install."""

    def rehearsal(self) -> dict:
        return {"unchanged": self.target}

    def live(self, rehearsal: dict) -> dict:
        self.installed.publish()
        PlannedInstallDeadline.after(self.installer, time.time()).plan(self.root)
        return {"unchanged": self.target}


class FreshInstall(Install):
    """Nothing was ever written at the root: no carry, rehearsal, hold or owner to restart.
    The target's source creates the bus already in its format and routes to it."""

    @classmethod
    def pending_steps(cls, root: Path, chain: CarryChain) -> tuple[Step, ...]:
        return ()

    @classmethod
    def of(cls, checkout: Path, root: Path, current: str, target: str, chain: CarryChain,
           pending: tuple[Step, ...], installed: Stack, staged: Stack, revisions: Revisions,
           installer: str, spill_dir: Path) -> Install:
        return cls(checkout, root, current, target, chain, pending, installed, staged,
                   revisions, installer, spill_dir)

    def rehearsal(self) -> dict:
        return {"fresh": str(self.root)}

    def live(self, rehearsal: dict) -> dict:
        run("git", "-C", self.checkout, "merge", "--ff-only", "-q", self.target)
        Revisions.prepare(self.checkout)
        self.staged.prepare_native()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        root_id = Comms(self.root).messaging.initialize_private_initial_protocol()
        CarryLedger(self.root).start(self.chain)
        self.staged.publish()
        route = route_new_bus(ActiveRoute(self.root.absolute(), root_id))
        services = HostService.restart_running(self.staged.python)
        PlannedInstallDeadline.after(self.installer, time.time()).plan(self.root)
        return {"created": str(self.root), "route": route, "services": services}


class RetiringInstall(FreshInstall):
    """The bus at the root is runtime state the target cannot carry: its owners stop, it
    moves aside intact, and a fresh bus takes its place."""

    def rehearsal(self) -> dict:
        return {"retire": str(self.root), **super().rehearsal()}

    def live(self, rehearsal: dict) -> dict:
        running = self.installed.running_owners(self.root)
        self.installed.owners(self.root, "stop", running)
        retired = self.root.with_name(f"{self.root.name}.retired-{time.strftime('%Y%m%dT%H%M%S')}")
        self.root.rename(retired)
        return {"retired": str(retired), "stopped": running, **super().live(rehearsal)}


def live(checkout: Path, root: Path, current: str, target: str, pending: tuple[Step, ...],
         installed: Stack, staged: Stack, revisions: Revisions, installer: str,
         spill_dir: Path, rehearsal_files: RehearsalFiles) -> dict:
    """The rehearsal's passes under the rehearsal's stack; ``installed`` only stops, starts
    and reads back its own owners."""
    expected = frozenset((*rehearsal_files.names, CarryLedger.name)) | frozenset(
        str(path.relative_to(root)) for path in staged.backup_files(root, pending, revisions))
    selected = tuple(root / name for name in sorted(expected)) if pending else ()
    restore = RestoreSpace.measure(max(
        rehearsal_files.restore_bytes,
        sum(path.stat().st_size for path in selected if path.exists()),
    ), root.parent)
    restore.require()
    stopping = before_stop(root, pending, staged, revisions)
    running = installed.running_owners(root)
    maintenance = MaintenanceBarrier(RegistryStore.at(root).path)
    held = maintenance.begin(MaintenanceOperator.current())
    os.environ[HOLDER_NONCE] = held.nonce  # this run's own CLI calls read under its hold
    try:
        installed.owners(root, "stop", running)
        with transcripts_held(root):
            before = bus_files(root)
            backup = (StoreBackup if selected else NoBackup).take(
                root, ROUTE, selected,
                spill_dir / f"{root.name}.upgrade-{time.strftime('%Y%m%dT%H%M%S')}")
    except BaseException as error:
        maintenance.clear(held)
        installed.owners(root, "start", running)
        print(f"install: aborted before the backup ({type(error).__name__}: {error}); "
              f"maintenance cleared and {running} restarted", file=sys.stderr)
        raise
    try:
        run("git", "-C", checkout, "merge", "--ff-only", "-q", target)
        Revisions.prepare(checkout)
        staged.prepare_native()
        passed = carry_and_read(root, before, pending, staged, staged, revisions, stopping)
        passed["files"].require_rehearsal(rehearsal_files)
        staged.publish()
        maintenance.clear(held)
        services = HostService.restart_running(staged.python)
        PlannedInstallDeadline.after(installer, time.time()).plan(root)
        try:
            staged.owners(root, "start", running)
        except BaseException:
            staged.owners(root, "stop", running)
            raise
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
            Revisions.prepare(checkout)
        # Republish only when current moved: the outgoing runtime may predate this publisher.
        if LaunchRuntime.installed().prefix.resolve() != installed.runtime.prefix.resolve():
            installed.publish()
        maintenance.clear(restored_hold)
        installed.owners(root, "start", running)
        print(f"install: restored after failure ({type(error).__name__}: {error}); "
              f"original runtime selected and {running} restarted; "
              f"backup: {backup.description}",
              file=sys.stderr)
        raise
    return {"stopped_and_started": running, **passed, "services": services,
            "restore_space": {"needed": restore.needed, **asdict(restore)},
            **backup.report()}
