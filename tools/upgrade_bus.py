#!/usr/bin/env python3
"""Upgrade a retained bus and the checkout that runs it, in one command.

    python tools/upgrade_bus.py --installer UNIT --spill-dir PATH
        [--checkout DIR] [--root ~/.agent-comms] [--to origin/main] [--rehearse]

``--checkout`` is the installation to upgrade (by default, the one holding
this file); run the command from the target revision so its steps are known.

The target revision's stack is built in a temporary worktree, relocked to the
latest OpenHCSDev mains, and must import Toad and the ACP client. The bus
ledger (``carries.json``) names the carry steps still pending; each runs from
the revision that declares it (``tools/cutover/carry.py``). Every run first
rehearses on a copy of the bus: the steps, then ``who``, ``threads`` and
``history`` reads, and every file no step names as rewritten must be
unchanged by saved-file size, nanosecond mtime and inode. The rehearsal
prepares native Pi in the staged stack exactly as the live phase does. ``--rehearse`` stops there.

A live run then stops the running agent owners with the installed CLI, backs
up only files the rehearsal changed, moves the checkout,
relocks and syncs its stack, prepares native Pi, carries the bus, reads it
back, and starts only the owners it stopped. A failure after the backup
keeps maintenance held and preserves the bus, checkout and backup for forward
repair. It never resets live main or replaces a possibly changed bus. Nothing
is sent and no input is replayed: owners start by the normal start path.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

# This file's tree is the target revision: the tool and every step it runs import that
# tree's src, never the src the checkout being upgraded has installed.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import publish_runtime
from cutover.backup import (
    RestoreSpace,
    StoreBackup,
    SavedFile,
    UpgradeRefused,
    bus_files,
    transcripts_held,
)
from cutover.carry import Carried, CarryChain, CarryLedger, Step, TodoColumnCarry
from cutover.runtime_resolution import RuntimeResolution
from install_gate import PracticeCopy, prepare_runtime_home

import agent_comms.activity_checkpoint  # noqa: F401  (declares the activity runtime files)
from agent_comms.comms import Comms
from agent_comms.external_transcript import ExternalTranscript
from agent_comms.field_codec import FieldCodec, Id128Text
from agent_comms.history_result import EverythingHistoryPageResult
from agent_comms.host_services import HostService
from agent_comms.launch_runtime import LaunchRuntime, RestartEnvironment, SpecifiedImportPath
from agent_comms.maintenance_barrier import HOLDER_NONCE, MaintenanceBarrier, MaintenanceOperator
from agent_comms.registry_store import RegistryStore
from agent_comms.store_files import _store_lock
from agent_comms.todos import PlannedInstallDeadline

REPO = Path(__file__).resolve().parents[1]
HOME = Path.home()
CLIENT_IMPORTS = "import toad.app, agent_comms.acp"
ROUTE = Path.home() / ".local/state/agent-comms/active-route.json"


@dataclass(frozen=True)
class RehearsalFiles:
    """The rehearsal's named rewrite set and the restore reservation it requires."""

    identical: int
    rewritten: int
    names: list[str]
    restore_bytes: int


def run(*command: str | Path, cwd: Path | None = None, env: dict | None = None) -> str:
    print("+", " ".join(map(str, command)), file=sys.stderr, flush=True)
    completed = subprocess.run([str(part) for part in command], cwd=cwd, env=env,
                               capture_output=True, text=True)
    if completed.returncode:
        raise UpgradeRefused(f"{command[0]} failed: {completed.stderr.strip() or completed.stdout.strip()}")
    return completed.stdout


def carried(root: Path, before: dict[str, SavedFile], steps: list[dict],
            expected: frozenset[str] = frozenset()) -> dict:
    """Only the files a step names as rewritten may differ from ``before``."""
    after = bus_files(root)
    physical = {name for name in before.keys() | after.keys()
                if before.get(name) != after.get(name)}
    # SQLite's committed WAL belongs to its database declaration, not a second carry.
    changed = {name[:-4] if name.endswith('-wal') and name[:-4] in before.keys() | after.keys()
               else name for name in physical}
    unnamed = sorted(changed - {CarryLedger.name}
                     - {name for step in steps for name in step["rewritten"]})
    if unnamed:
        raise UpgradeRefused(f"Carries changed files no step names: {unnamed}")
    outside = sorted(changed - expected) if expected else []
    if outside:
        raise UpgradeRefused(f"Live carries changed files absent from rehearsal: {outside}")
    return asdict(RehearsalFiles(
        identical=len(before.keys() - changed), rewritten=len(changed), names=sorted(changed),
        restore_bytes=sum(before[name].size for name in physical & before.keys())))


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

    def select_route(self, root: Path) -> None:
        """Validate and publish through this stack's prepared native package."""
        run(self.python, "-c", """import sys
from pathlib import Path
from agent_comms.active_route import ActiveRoute, publish_active_route
from agent_comms.wire_log import WireLog
root = Path(sys.argv[1])
marker = WireLog(root / 'bus.jsonl').private_marker_unlocked()
publish_active_route(ActiveRoute(root, marker.root_id))
""", root, cwd=self.checkout, env=self.environment())

    def publish(self) -> None:
        """The selected interpreter owns console metadata and harness declarations."""
        run(
            self.python, publish_runtime.__file__, "--refresh",
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

    def tree(self, revision: str) -> Path:
        if revision not in self.trees:
            path = self.scratch / f"tree-{revision[:12]}"
            run("git", "-C", self.repo, "worktree", "add", "--detach", "-q", path, revision)
            self.trees[revision] = path
        path = self.trees[revision]
        self.prepare(path)
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
    tree = revisions.tree(step.family.source_revision(step))
    environment = stack.environment(tree / "src", tree / "tools")
    worker = (
        "import importlib,json,sys; from dataclasses import asdict; from pathlib import Path; "
        "step=getattr(importlib.import_module(sys.argv[1]),sys.argv[2]); "
        "print(json.dumps(asdict(getattr(step,sys.argv[3])(Path(sys.argv[4]),apply=True))))"
    )
    operation = "before_stop" if "--before-stop" in flags else "carry"
    output = run(stack.python, "-c", worker, f"cutover.{step.name}", step.declaration,
                 operation, root, cwd=tree, env=environment)
    return FieldCodec.decode(Carried, FieldCodec.parse(output.strip().splitlines()[-1]))


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
    source = Comms(root)
    parent = source.registry.storage_rule.initial_roots(installer).scratch
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (
        tempfile.TemporaryDirectory(dir=parent, prefix="practice-") as scratch,
        PracticeCopy(source=source, into=Path(scratch) / "bus").open() as (copied, measurement),
    ):
        home = Path(scratch) / "home"
        prepare_runtime_home(home)
        (home / ".claude").symlink_to(Path.home() / ".claude")
        stack = replace(stack, home=home)
        stack.prepare_native()
        target = run("git", "-C", stack.checkout, "rev-parse", "HEAD").strip()
        pending = CarryChain.at(revisions.repo, target).pending(CarryLedger(copied.root).applied())
        stopping = before_stop(copied.root, pending, stack, revisions)
        stack.select_route(copied.root)
        os.environ[HOLDER_NONCE] = MaintenanceBarrier(copied.root / "registry.json").begin(
            MaintenanceOperator.current()).nonce
        with transcripts_held(copied.root):
            before = bus_files(copied.root)
        report = carry_and_read(copied.root, before, pending, stack, stack, revisions, stopping)
        return {**report, "copy": asdict(measurement)}


def carry_and_read(root: Path, before: dict[str, SavedFile], pending: tuple[Step, ...],
                   staged: Stack, reader: Stack, revisions: Revisions,
                   stopping: list[dict], expected: frozenset[str] = frozenset()) -> dict:
    """The pass a rehearsal and a live install share once ``before`` was read under the
    transcript hold. The hold is released first: SQLite cannot lend its writer lock to a
    step's process, so a step that writes the transcript store could not run under it."""
    steps = [*stopping, *carry_all(root, pending, staged, revisions)]
    return {"steps": steps, "files": carried(root, before, steps, expected),
            "read": reader.verify(root)}


def upgrade(checkout: Path, root: Path, target: str, installer: str, rehearse_only: bool,
            spill_dir: Path) -> dict:
    current = run("git", "-C", checkout, "rev-parse", "HEAD").strip()
    run("git", "-C", checkout, "fetch", "-q", "origin")
    target = run("git", "-C", checkout, "rev-parse", "--verify", f"{target}^{{commit}}").strip()
    if subprocess.run(["git", "-C", str(checkout), "merge-base", "--is-ancestor", current, target]).returncode:
        raise UpgradeRefused(f"{target[:12]} does not follow the checkout's {current[:12]}")
    if run("git", "-C", checkout, "status", "--porcelain", "--untracked-files=no").strip():
        raise UpgradeRefused("The checkout has local changes")
    applied = CarryLedger(root).applied()
    try:
        pending = CarryChain.at(checkout, target).pending(applied)
    except ValueError as error:
        raise UpgradeRefused(str(error)) from None
    installed = Stack(checkout)
    installed.require_uv()
    if not rehearse_only and not installed.python.exists():
        raise UpgradeRefused(f"No installed stack at {installed.python}")
    with tempfile.TemporaryDirectory(prefix="upgrade-bus.") as scratch:
        revisions = Revisions(checkout, Path(scratch))
        try:
            staged = Stack.build(revisions.tree(target))
            install = Install.of(checkout, root, current, target, pending, installed, staged, revisions,
                                 installer, spill_dir)
            rehearsal = install.rehearsal()
            report = {"from": current, "to": target, "pending": [step.name for step in pending],
                      "rehearsal": rehearsal}
            if rehearse_only:
                return report
            report["live"] = install.live(rehearsal)
            return report
        finally:
            revisions.close()


@dataclass(frozen=True)
class Install(ABC):
    """One run once the target's stack is relocked: what it changes on the bus."""

    checkout: Path
    root: Path
    current: str
    target: str
    pending: tuple[Step, ...]
    installed: Stack
    staged: Stack
    revisions: Revisions
    installer: str
    spill_dir: Path

    @classmethod
    def of(cls, checkout: Path, root: Path, current: str, target: str, pending: tuple[Step, ...],
           installed: Stack, staged: Stack, revisions: Revisions, installer: str,
           spill_dir: Path) -> Install:
        unchanged = not pending and current == target and RuntimeResolution.installed().matches(
            staged.lock, staged.python, target)
        member = (ChangedInstall, UnchangedInstall)[unchanged]
        return member(checkout, root, current, target, pending, installed, staged, revisions,
                      installer, spill_dir)

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


def live(checkout: Path, root: Path, current: str, target: str, pending: tuple[Step, ...],
         installed: Stack, staged: Stack, revisions: Revisions, installer: str,
         spill_dir: Path, rehearsal_files: dict) -> dict:
    """The rehearsal's passes under the rehearsal's stack; ``installed`` only stops, starts
    and reads back its own owners."""
    files = RehearsalFiles(**rehearsal_files)
    restore = RestoreSpace.measure(files.restore_bytes, root.parent)
    expected = frozenset((*files.names, CarryLedger.name))
    selected = tuple(root / name for name in sorted(expected)) if pending else ()
    restore.require()
    stopping = before_stop(root, pending, staged, revisions)
    running = staged.running_owners(root)
    maintenance = MaintenanceBarrier(RegistryStore.at(root).path)
    held = maintenance.begin(MaintenanceOperator.current())
    os.environ[HOLDER_NONCE] = held.nonce  # this run's own CLI calls read under its hold
    try:
        staged.owners(root, "stop", running)
        with transcripts_held(root):
            before = bus_files(root)
            backup = (StoreBackup.take(root, ROUTE, selected,
                spill_dir / f"{root.name}.upgrade-{time.strftime('%Y%m%dT%H%M%S')}")
                if selected else None)
    except BaseException as error:
        maintenance.clear(held)
        installed.owners(root, "start", running)
        print(f"upgrade-bus: aborted before the backup ({type(error).__name__}: {error}); "
              f"maintenance cleared and {running} restarted", file=sys.stderr)
        raise
    try:
        run("git", "-C", checkout, "merge", "--ff-only", "-q", target)
        Revisions.prepare(checkout)
        staged.prepare_native()
        passed = carry_and_read(root, before, pending, staged, staged, revisions, stopping,
                                expected)
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
        state = maintenance.read()
        recovery = state.phase.hold_for(maintenance, state, MaintenanceOperator.current())
        os.environ[HOLDER_NONCE] = recovery.nonce
        if backup and not backup.original():
            backup.restore()
        restored = maintenance.read()
        restored_hold = restored.phase.hold_for(maintenance, restored, MaintenanceOperator.current())
        os.environ[HOLDER_NONCE] = restored_hold.nonce
        installed.publish()
        maintenance.clear(restored_hold)
        installed.owners(root, "start", running)
        print(f"upgrade-bus: restored after failure ({type(error).__name__}: {error}); "
              f"original runtime selected and {running} restarted; backup: {backup.path if backup else None}",
              file=sys.stderr)
        raise
    return {"stopped_and_started": running, **passed, "services": services,
            "restore_space": {"needed": restore.needed, **asdict(restore)},
            "backup": str(backup.path) if backup else None,
            "backup_bytes": sum(path.stat().st_size for path in backup.data_root.rglob('*')
                                if path.is_file()) if backup else 0}


def detached_live(command: tuple[str, ...], root: Path) -> int:
    """The live installer is a new session, not the monitor's owned child group.

    Its output descriptors belong to files, not a caller that may disappear.
    Deliberately do not borrow a turn-owned child lifetime or use Popen's cleanup
    context: retiring the observer must not terminate this independent install.
    """
    logs = root.parent / f".{root.name}.installer-{Id128Text.mint()}"
    logs.mkdir(mode=0o700)
    output, errors = logs / "result.json", logs / "stderr.log"
    with output.open("xb") as stdout, errors.open("xb") as stderr:
        process = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
            start_new_session=True,
        )
    print(f"upgrade-bus: detached installer pid={process.pid}; diagnostics {errors}",
          file=sys.stderr, flush=True)
    code = process.wait()
    if code:
        print(f"upgrade-bus: detached installer failed ({code}); see {errors}", file=sys.stderr)
    else:
        print(output.read_text(), end="")
    return code


def stopped(signum: int, frame: object) -> None:
    """A stop signal runs the same abort as a failure; a second one cannot cut that abort short."""
    for each in (signal.SIGTERM, signal.SIGINT):
        signal.signal(each, signal.SIG_IGN)
    raise SystemExit(f"upgrade-bus: stopped by {signal.Signals(signum).name}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--checkout", type=lambda value: Path(value).expanduser().resolve(), default=REPO)
    parser.add_argument("--root", type=lambda value: Path(value).expanduser().resolve(),
                        default=Path.home() / ".agent-comms")
    parser.add_argument("--to", default="origin/main")
    parser.add_argument("--installer", required=True,
                        help="The unit running this install; the plan names it")
    parser.add_argument("--rehearse", action="store_true", help="Carry and read a copy of the bus; change nothing")
    parser.add_argument("--spill-dir", required=True,
                        type=lambda value: Path(value).expanduser().resolve(),
                        help="Explicit parent directory for rehearsal copies and live backups")
    args = parser.parse_args()
    if not args.rehearse and os.getsid(0) != os.getpid():
        raise SystemExit(detached_live(
            (sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]), args.root,
        ))
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, stopped)
    try:
        print(json.dumps(upgrade(args.checkout, args.root, args.to, args.installer, args.rehearse,
                                 args.spill_dir), indent=2))
    except UpgradeRefused as error:
        raise SystemExit(error.output)


if __name__ == "__main__":
    main()
