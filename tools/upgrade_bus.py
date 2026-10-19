#!/usr/bin/env python3
"""Install a target revision of agent-comms on a retained bus, in one command.

    python tools/upgrade_bus.py --installer UNIT [--repo DIR] [--root ~/.agent-comms]
        [--to origin/main] [--rehearse]

What runs is one immutable runtime. ``~/.local/share/agent-comms/current`` names a
directory under ``runtimes/``: a virtual environment holding Core built from a clean
worktree of the target revision, the dependency set the stack project resolves
(OpenHCSDev packages at their mains), and that revision's native Pi. Every program,
launcher and skill link reads through ``current``; nothing runs a checkout, so ``--repo``
is only where revisions come from.

Every run builds that runtime (a runtime the same inputs built is reused), then
rehearses on a copy of the bus. The ledger (``carries.json``) names the carry steps
still pending; each runs from the revision that declares it (``tools/cutover/carry.py``)
under the new runtime's Python and the wire lock, and every file it does not name as
rewritten must be byte-identical. Then ``who``, ``threads`` and ``history`` read the
bus. A thread of the installed runtime is in a turn throughout: it is queued to restart
onto the new runtime, and does so only once that turn ends. Last, the runtime checks
itself on the copy: a new thread answers a DM while Toad shows it
(``tools/runtime_check.py``, which drives Toad's ``tools/acceptance.py``).
``--rehearse`` stops there.

A live run never waits for owners. It backs the bus up (tar plus a hash of every file)
while no writer can change it, carries it, reads it back, moves every link and then
``current`` to the new runtime and restarts the host services onto it. Each running
owner is then queued to restart onto the new runtime when its own turn ends
(``agent_comms.restart_queue``); the report names each queued restart, and
``comms_restart_queue`` shows how each settled. Runtimes no process uses, other than the
new and the previous one, are removed. A failure after the backup holds maintenance and
preserves the bus, the previous runtime and the backup for forward repair. Nothing is
sent and no input is replayed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager, suppress
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from typing import ClassVar

# This file's tree is the target revision: the tool and every step it runs import that
# tree's src, never the src of the runtime being replaced.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cutover.backup import Backup, UpgradeRefused, bus_files, transcripts_held
from cutover.carry import Carried, Carry, CarryChain, CarryLedger, Step, TodoColumnCarry
from runtime_check import DmPage, Named, RuntimeChecked, known

import agent_comms.activity_checkpoint  # noqa: F401  (declares the activity runtime files)
import agent_comms.cli  # noqa: F401  (imports every owner, so each declared bus location is known)
from agent_comms.active_route import active_route_path, publish_active_route, read_active_route
from agent_comms.coordination_database import CoordinationStore
from agent_comms.field_codec import FieldCodec
from agent_comms.harness_install import replace_link
from agent_comms.history_result import EverythingHistoryPageResult
from agent_comms.host_services import HostService
from agent_comms.launch_runtime import (
    ImportPath,
    NoImportPath,
    RestartEnvironment,
    SpecifiedImportPath,
    ThreadLaunch,
)
from agent_comms.locked_store import HistoryDirectory
from agent_comms.maintenance_barrier import MaintenanceBarrier, MaintenanceOperator, ReadyPhase
from agent_comms.messages import Message
from agent_comms.native_arguments import NativeArguments
from agent_comms.owner_cutover import StoppedOwnerInstallation
from agent_comms.registry_store import RegistryStore
from agent_comms.restart_queue import QueuedRestart
from agent_comms.storage_rules import StorageRule
from agent_comms.store_files import _atomic_write_text, _store_lock
from agent_comms.todos import PlannedInstallDeadline
from agent_comms.tools import CommsQueueRestartTool, RestartQueued
from agent_comms.wire_log import WireLog

REPO = Path(__file__).resolve().parents[1]
CLIENT_IMPORTS = "import toad.app, agent_comms.acp"
ROUTE = active_route_path()
PI = Path.home() / ".pi/agent"
CURRENT = StoppedOwnerInstallation.current_pointer()
RUNTIMES = CURRENT.parent / "runtimes"
UV = Path.home() / ".local/state/agent-comms/upgrade-bus/uv"
"""The uv the installer runs, kept in the installer's own state."""


def run(*command: str | Path, cwd: Path | None = None, env: Mapping[str, str] = os.environ) -> str:
    print("+", " ".join(map(str, command)), file=sys.stderr, flush=True)
    completed = subprocess.run(
        [str(part) for part in command], cwd=cwd, env=env, capture_output=True, text=True
    )
    if completed.returncode:
        raise UpgradeRefused(
            f"{command[0]} failed: {completed.stderr.strip() or completed.stdout.strip()}"
        )
    return completed.stdout


@dataclass(frozen=True)
class FilesCompared:
    """How a step's files compare with the bus before it ran."""

    identical: int
    rewritten: int


@dataclass(frozen=True)
class CarriedStep:
    """One step, applied, and the files it left as they were."""

    carry: str
    carried: Carried
    files: FilesCompared


def carried(root: Path, before: dict[str, str], steps: tuple[Carried, ...]) -> FilesCompared:
    """Only the files a step names as rewritten may differ from ``before``."""
    after = bus_files(root)
    changed = {name for name in before.keys() | after.keys() if before.get(name) != after.get(name)}
    unnamed = sorted(changed - {name for step in steps for name in step.rewritten})
    if unnamed:
        raise UpgradeRefused(f"Carries changed files no step names: {unnamed}")
    return FilesCompared(len(before.keys() - changed), len(changed))


def stepped(root: Path, step: Step, apply: Callable[[], Carried]) -> CarriedStep:
    """One step under the wire lock, which every writer of the bus takes, so the files it
    changes are the files it names even while owners run. The transcript store's writers
    take SQLite's lock instead: it is held while ``before`` is read, never across the step,
    which may write that store itself."""
    with _store_lock(root / "wire"):
        with transcripts_held(root):
            before = bus_files(root)
        applied = apply()
        return CarriedStep(step.name, applied, carried(root, before, (applied,)))


@dataclass(frozen=True)
class Home:
    """The home a runtime's clients read their route and settings from."""

    path: Path = Path.home()

    def declared(self, environment: RestartEnvironment) -> RestartEnvironment:
        return replace(environment, home=str(self.path))

    def environment(self, inherited: Mapping[str, str], imported: ImportPath) -> Mapping[str, str]:
        """``inherited`` with this home's declared variables, ``imported`` its only import path."""
        declared = replace(
            self.declared(RestartEnvironment.inherit(inherited)), pythonpath=imported
        )
        return {**RestartEnvironment.strip(dict(inherited)), **declared.encode()}


@dataclass(frozen=True)
class RehearsalHome(Home):
    """A private home whose route selects the copy wherever the real one selected the bus.
    Pi reads the owner's real credentials and keeps every other file private to it."""

    pi: Path = Path()

    @classmethod
    def make(cls, scratch: Path, root: Path, copy: Path) -> RehearsalHome:
        home = scratch / "home"
        route = home / ROUTE.relative_to(Path.home())
        route.parent.mkdir(mode=0o700, parents=True)
        if current := read_active_route(ROUTE):
            publish_active_route(
                replace(current, root=copy) if current.root == root.resolve() else current, route
            )
        pi = scratch / "pi"
        pi.mkdir(mode=0o700)
        settings = PI / "settings.json"
        if settings.exists():
            shutil.copy2(settings, pi / "settings.json")
        return cls(home, pi)

    def declared(self, environment: RestartEnvironment) -> RestartEnvironment:
        # The installer's own root is not the copy's.
        return replace(
            super().declared(environment), root=None, native_config=PI, agent_directory=self.pi
        )

    def environment(self, inherited: Mapping[str, str], imported: ImportPath) -> Mapping[str, str]:
        # Nor is its own thread.
        return ThreadLaunch.strip(super().environment(inherited, imported))


@dataclass(frozen=True)
class BusRead:
    """How much of the bus each of the new runtime's reads returned."""

    threads: int
    who: int
    history: int


@dataclass(frozen=True)
class ThreadsRead:
    threads: tuple[Named, ...]


@dataclass(frozen=True)
class WhoRead:
    who: tuple[Named, ...]


@dataclass(frozen=True)
class EverythingRead:
    messages: tuple[Message, ...] = field(
        metadata={"wire_name": EverythingHistoryPageResult.messages_key}
    )


@dataclass(frozen=True)
class Runtime:
    """One installed runtime: the Python and CLI an installation runs."""

    path: Path
    home: Home = Home()
    record_name = "build.json"

    @classmethod
    def current(cls) -> Runtime:
        return cls(CURRENT.resolve(strict=True))

    @property
    def python(self) -> Path:
        return self.path / "bin/python"

    @property
    def record(self) -> Path:
        return self.path / self.record_name

    def environment(self, *paths: Path) -> Mapping[str, str]:
        """``paths`` alone are importable beside the runtime's own packages; ``home``
        holds the route its clients read."""
        imported = (
            SpecifiedImportPath(os.pathsep.join(map(str, paths))) if paths else NoImportPath()
        )
        return {
            **self.home.environment(os.environ, imported),
            NativeArguments.binary_key: str(self.python.with_name("pi-comms-native")),
        }

    def report(self, code: str, *arguments: str | Path) -> str:
        """One answer the runtime gives about itself, from its own packages only."""
        return run(self.python, "-I", "-c", code, *arguments, env=self.environment()).strip()

    def native_package(self) -> Path:
        return Path(
            self.report(
                "from agent_comms.native_package import pinned_location; "
                "print(pinned_location())"
            )
        )

    def command(self, root: Path, *arguments: str) -> str:
        """One CLI command for this bus alone, never the route's, waiting while the route
        index is still being built."""
        for _ in range(240):
            completed = subprocess.run(
                [
                    str(self.python),
                    "-c",
                    "from agent_comms.cli import main; main()",
                    "--root",
                    str(root),
                    *arguments,
                ],
                cwd=self.path,
                env=self.environment(),
                capture_output=True,
                text=True,
            )
            if "Route index covers" not in completed.stdout:
                break
            time.sleep(5)
        if completed.returncode:
            raise UpgradeRefused(
                f"agent-comms {' '.join(arguments)}: "
                f"{completed.stdout.strip() or completed.stderr.strip()}"
            )
        return completed.stdout

    def read[R](self, root: Path, record: type[R], *arguments: str) -> R:
        """One CLI result, decoded once into the fields ``record`` declares."""
        return known(record, FieldCodec.parse(self.command(root, *arguments)))

    def owners(self, root: Path) -> tuple[str, ...]:
        """The owners a restart replaces, as this runtime's Core selects them."""
        return tuple(
            self.report(
                "import sys; from pathlib import Path; from agent_comms.comms import wire; "
                "snapshot = wire(Path(sys.argv[1])).registry.snapshot(); "
                "print(*(candidate.name for thread in snapshot.threads.values() "
                "for candidate in thread.restart_candidates(snapshot.statuses[thread.name])))",
                root,
            ).split()
        )

    def queue_restart(self, root: Path, name: str) -> QueuedRestart:
        """Restart this owner onto this runtime when its own turn ends; never interrupts it."""
        return self.read(
            root,
            RestartQueued,
            "tool",
            "--tool",
            CommsQueueRestartTool.declared_name,
            "--arguments",
            json.dumps({"name": name}),
        ).restart

    def queue_restarts(self, root: Path) -> tuple[QueuedRestart, ...]:
        """Every independent owner gets its request concurrently, not one stop delay at a time."""
        with ThreadPoolExecutor() as requests:
            return tuple(requests.map(partial(self.queue_restart, root), self.owners(root)))

    def settled(self, root: Path, queued: QueuedRestart, patience: int) -> QueuedRestart:
        """The queued restart once it settles; a settled restart names the owner's
        previous process and the one that replaced it."""
        return FieldCodec.decode(
            QueuedRestart,
            FieldCodec.parse(
                run(
                    self.python,
                    "-I",
                    Path(__file__).with_name("restart_settled.py"),
                    root,
                    queued.id,
                    str(patience),
                    env=self.environment(),
                )
            ),
        )

    def scratch_base(self, root: Path) -> Path:
        """Where this bus's clients keep scratch, as this runtime's home reads it."""
        return Path(
            self.report(
                "import sys; from pathlib import Path; "
                "from agent_comms.storage_rules import StorageRule; "
                "print(StorageRule.load(Path(sys.argv[1])).scratch_base)",
                root,
            )
        )

    def user(self, root: Path) -> str:
        return self.report(
            "import sys; from pathlib import Path; from agent_comms.comms import wire; "
            "print(wire(Path(sys.argv[1])).registry.user_thread()"
            ".require(RuntimeError('the bus has no user')).name)",
            root,
        )

    def verify(self, root: Path) -> BusRead:
        return BusRead(
            len(self.read(root, ThreadsRead, "threads").threads),
            len(self.read(root, WhoRead, "who").who),
            len(self.read(root, EverythingRead, "history", "--limit", "50").messages),
        )


INVENTORY = """import importlib.metadata, json
print(json.dumps({d.metadata['Name'].lower().replace('_', '-'): {
    'version': d.version, 'source': json.loads(d.read_text('direct_url.json') or 'null')}
    for d in importlib.metadata.distributions()}))"""


@dataclass(frozen=True)
class RuntimeBuild:
    """The runtime one clean worktree of the target revision builds. Its name is its
    inputs: the Core commit, the resolved dependency set and the native pin."""

    tree: Path
    runtimes: Path = RUNTIMES

    def requirements(self) -> str:
        """OpenHCSDev packages track their mains; the lock only records this resolution."""
        run(UV, "lock", "--project", "stack", "--upgrade", "--python", "3.14", cwd=self.tree)
        return run(
            UV,
            "export",
            "--project",
            "stack",
            "--frozen",
            "--no-dev",
            "--no-header",
            "--no-annotate",
            "--no-emit-project",
            "--no-emit-package",
            "agent-comms",
            cwd=self.tree,
        )

    def build(self) -> Runtime:
        requirements = self.requirements()
        commit = run("git", "-C", self.tree, "rev-parse", "HEAD").strip()
        pin = hashlib.sha256((self.tree / "stack/pi-native.sha256").read_bytes()).hexdigest()
        dependencies = hashlib.sha256(requirements.encode()).hexdigest()
        runtime = Runtime(
            self.runtimes / f"core-{commit[:9]}-deps-{dependencies[:12]}-native-{pin[:16]}"
        )
        if runtime.record.is_file():
            return runtime
        if runtime.path.exists():
            # An unfinished build of these same inputs; nothing runs a runtime without a record.
            shutil.rmtree(runtime.path)
        self.runtimes.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self.runtimes, prefix=".wheel-") as scratch:
            wheels = Path(scratch)
            run(UV, "build", "--wheel", "--out-dir", wheels, self.tree)
            (wheel,) = wheels.glob("*.whl")
            (wheels / "requirements.txt").write_text(requirements)
            run(UV, "venv", "--python", "3.14", runtime.path)
            run(
                UV,
                "pip",
                "install",
                "--python",
                runtime.python,
                "--no-deps",
                "-r",
                wheels / "requirements.txt",
                wheel,
            )
        package = Path(runtime.report("import agent_comms; print(agent_comms.__path__[0])"))
        if (package / "package-lock.json").is_file():
            run(
                "npm",
                "ci",
                "--ignore-scripts",
                "--omit=dev",
                "--no-audit",
                "--no-fund",
                cwd=package,
            )
        run(UV, "pip", "check", "--python", runtime.python)
        self.native(runtime)
        run(runtime.path / "bin/toad", "--version")
        runtime.report(CLIENT_IMPORTS)
        record = {
            "core_commit": commit,
            "native_manifest_sha256": pin,
            "requirements": requirements,
            "packages": json.loads(runtime.report(INVENTORY)),
        }
        _atomic_write_text(runtime.record, json.dumps(record, indent=2) + "\n", fsync_parent=True)
        return runtime

    def native(self, runtime: Runtime) -> None:
        """The revision's own preparation builds native Pi; the runtime keeps the result."""
        run(self.tree / "stack/bin/prepare-pi-native", "--python", runtime.python, cwd=self.tree)
        built = runtime.native_package().parents[2]
        built.parent.mkdir(parents=True, exist_ok=True)
        os.rename(self.tree / "stack" / built.name, built)
        runtime.report(
            "from agent_comms.native_package import pinned_native_package, "
            "verify_native_package; verify_native_package(pinned_native_package())"
        )


@dataclass(frozen=True)
class VcsCommit:
    commit_id: str


@dataclass(frozen=True)
class DirectSource:
    """Where pip installed a package from, as its ``direct_url.json`` records it."""

    url: str
    vcs_info: VcsCommit


@dataclass(frozen=True)
class InstalledPackage:
    source: DirectSource


@dataclass(frozen=True)
class ToadPackages:
    toad: InstalledPackage = field(metadata={"wire_name": "batrachian-toad"})


@dataclass(frozen=True)
class BuildRecord:
    """The part of a runtime's ``build.json`` the installer reads back."""

    packages: ToadPackages


@dataclass(frozen=True)
class ToadSource:
    """The Toad commit a runtime installed, fetched for the checks Toad keeps in its tools."""

    url: str
    commit: str

    @classmethod
    def of(cls, runtime: Runtime) -> ToadSource:
        source = known(
            BuildRecord, FieldCodec.parse(runtime.record.read_text())
        ).packages.toad.source
        return cls(source.url, source.vcs_info.commit_id)

    def fetch(self, directory: Path) -> Path:
        run("git", "init", "-q", directory)
        run("git", "-C", directory, "fetch", "-q", "--depth", "1", self.url, self.commit)
        run("git", "-C", directory, "checkout", "-q", "--detach", "FETCH_HEAD")
        return directory


@dataclass(frozen=True)
class RuntimeCheck:
    """The new runtime's own check, in a check root the carried copy builds from one
    thread's recent history: a fresh thread there answers a DM, and Toad shows it."""

    runtime: Runtime
    tree: Path

    def run(self, copy: Path, scratch: Path, thread: str) -> RuntimeChecked:
        toad = ToadSource.of(self.runtime).fetch(scratch / "toad")
        output = scratch / "check"
        into = self.runtime.scratch_base(copy) / "install-check" / scratch.name
        run(
            self.runtime.python,
            "-c",
            "from agent_comms.cli import main; main()",
            "--root",
            copy,
            "check-root",
            "--threads",
            thread,
            "--into",
            into,
            "--",
            self.runtime.python,
            "-I",
            self.tree / "tools/runtime_check.py",
            "--root",
            "{root}",
            "--toad",
            toad,
            "--output",
            output,
            cwd=scratch,
            env=self.runtime.environment(),
        )
        return RuntimeChecked.read(output)


@dataclass
class Revisions:
    """Temporary worktrees of the revisions the target and its pending steps come from."""

    repo: Path
    scratch: Path

    def __post_init__(self) -> None:
        self.trees: dict[str, Path] = {}

    def tree(self, revision: str) -> Path:
        if revision not in self.trees:
            path = self.scratch / f"tree-{revision[:12]}"
            run("git", "-C", self.repo, "worktree", "add", "--detach", "-q", path, revision)
            self.trees[revision] = path
        return self.trees[revision]

    def close(self) -> None:
        for path in self.trees.values():
            subprocess.run(
                ["git", "-C", str(self.repo), "worktree", "remove", "--force", str(path)]
            )


WORKER = (
    "import importlib,json,sys; from dataclasses import asdict; from pathlib import Path; "
    "step=getattr(importlib.import_module(sys.argv[1]),sys.argv[2]); "
    "print(json.dumps(asdict(getattr(step,sys.argv[3])(Path(sys.argv[4]),apply=True))))"
)
"""Runs one operation of a step from the revision that declares it."""


class CarryPass(ABC):
    """One of the two operations every step declares, run on every pending step in order."""

    operation: ClassVar[Callable[..., Carried]]

    def run(self, step: Step, root: Path, runtime: Runtime, revisions: Revisions) -> Carried:
        """Run one step from the revision that declares it, with that revision's src and the
        new runtime's Python, never the one being replaced."""
        if step.columns:
            return self.columns(step, root, revisions)
        tree = revisions.tree(step.revision)
        output = run(
            runtime.python,
            "-c",
            WORKER,
            f"cutover.{step.name}",
            step.declaration,
            self.operation.__name__,
            root,
            cwd=tree,
            env=runtime.environment(tree / "src", tree / "tools"),
        )
        return FieldCodec.decode(Carried, FieldCodec.parse(output.strip().splitlines()[-1]))

    @abstractmethod
    def columns(self, step: Step, root: Path, revisions: Revisions) -> Carried:
        """A todo column step, which this tree's own declaration applies."""

    @abstractmethod
    def all(
        self, root: Path, pending: tuple[Step, ...], runtime: Runtime, revisions: Revisions
    ) -> tuple[CarriedStep, ...]: ...


class BeforeStop(CarryPass):
    """What the running installation needs before its owners stop and read the bus."""

    operation = Carry.before_stop

    def columns(self, step: Step, root: Path, revisions: Revisions) -> Carried:
        return TodoColumnCarry.before_stop(root, apply=True)

    def all(
        self, root: Path, pending: tuple[Step, ...], runtime: Runtime, revisions: Revisions
    ) -> tuple[CarriedStep, ...]:
        return tuple(
            stepped(root, step, partial(self.run, step, root, runtime, revisions))
            for step in pending
        )


class CarryOver(CarryPass):
    """Each step's carry, recorded in the ledger as it applies."""

    operation = Carry.carry

    def columns(self, step: Step, root: Path, revisions: Revisions) -> Carried:
        return TodoColumnCarry.carry_step(
            root, step, CarryChain.at(revisions.repo, step.revision), apply=True
        )

    def all(
        self, root: Path, pending: tuple[Step, ...], runtime: Runtime, revisions: Revisions
    ) -> tuple[CarriedStep, ...]:
        return tuple(
            stepped(
                root,
                step,
                partial(
                    CarryLedger(root).apply,
                    CarryChain.at(revisions.repo, step.revision),
                    step.name,
                    partial(self.run, step, root, runtime, revisions),
                ),
            )
            for step in pending
        )


@contextmanager
def held(root: Path) -> Iterator[None]:
    """No writer changes the bus while its files are read whole: every writer takes the
    wire lock, and a reader repairing the log's checkpoint takes the log's lock."""
    with _store_lock(root / "wire"), WireLog.at(root).locked():
        yield


def snapshot_store(source: Path, target: Path) -> None:
    """One consistent database, read in a single transaction by sqlite's backup: units
    keep writing their databases outside the wire lock, so their files are never copied."""
    with (
        closing(CoordinationStore.read_snapshot(source)) as database,
        closing(sqlite3.connect(target)) as snapshot,
    ):
        database.backup(snapshot)
    shutil.copystat(source, target)


@dataclass(frozen=True)
class CopySize:
    """What a copy needed before it was taken, and what it holds on disk."""

    needed: int
    held: int


@dataclass(frozen=True)
class BusSnapshot:
    """A consistent snapshot of a bus its units keep writing. The log and every file a
    writer changes under the wire lock are copied holding the wire and log locks; each
    database is read whole by sqlite's backup, and its write-ahead files are not copied.
    Only files, directories and links are copied, so sockets and names already gone are
    skipped, and no history directory: starting the bus and one fresh turn never open
    one. An open barrier's witness names the original root, and a copy holding it
    would read as inconsistent, so an open copy starts as a bus that never held maintenance."""

    root: Path
    witness: MaintenanceBarrier
    left: frozenset[Path]
    """What the root holds that no copy takes: the barrier's witness and every history directory."""

    @classmethod
    def of(cls, root: Path) -> BusSnapshot:
        witness = MaintenanceBarrier(RegistryStore.at(root).path)
        return cls(
            root, witness, frozenset((*witness.witness_paths, *HistoryDirectory.paths_in(root)))
        )

    @staticmethod
    def mode(path: Path) -> int:
        # Presence files are replaced outside the wire lock; a name renamed away
        # between listing and copying is not part of the bus.
        try:
            return path.lstat().st_mode
        except FileNotFoundError:
            return 0

    @classmethod
    def store(cls, path: Path) -> bool:
        return stat.S_ISREG(cls.mode(path)) and CoordinationStore.is_database(path)

    def skipped(self, directory: str, names: list[str]) -> set[str]:
        paths = {name: Path(directory, name) for name in names}
        skip = {
            name
            for name, path in paths.items()
            if not stat.S_ISREG(mode := self.mode(path))
            and not stat.S_ISDIR(mode)
            and not stat.S_ISLNK(mode)
        }
        skip |= {
            companion.name
            for path in paths.values()
            if self.store(path)
            for companion in CoordinationStore.files(path)[1:]
        }
        return skip | {name for name, path in paths.items() if path in self.left}

    def needed(self) -> int:
        """The bytes the copy takes: every file it copies, and each database as large as its
        files hold it, write-ahead pages included."""
        total = 0
        for directory, directories, names in os.walk(self.root):
            skip = self.skipped(directory, [*directories, *names])
            directories[:] = [name for name in directories if name not in skip]
            for path in (Path(directory, name) for name in names if name not in skip):
                parts = CoordinationStore.files(path) if self.store(path) else (path,)
                total += sum(part.lstat().st_size for part in parts if self.mode(part))
        return total

    def present(self, source: str, target: str) -> None:
        with suppress(FileNotFoundError):
            (snapshot_store if self.store(Path(source)) else shutil.copy2)(
                Path(source), Path(target)
            )

    def copy(self, copy: Path) -> CopySize:
        """Refuses before writing anything when the copy's filesystem cannot hold it."""
        with held(self.root):
            if self.witness.current_unlocked().phase is not ReadyPhase:
                raise UpgradeRefused("Maintenance holds the bus; finish or repair that first")
            needed, free = self.needed(), shutil.disk_usage(copy.parent).free
            if needed > free:
                raise UpgradeRefused(
                    f"A copy of {self.root} needs {needed} bytes; "
                    f"{copy.parent} has {free} bytes free"
                )
            shutil.copytree(
                self.root, copy, symlinks=True, ignore=self.skipped, copy_function=self.present
            )
        return CopySize(needed, sum(path.lstat().st_blocks * 512 for path in copy.rglob("*")))


@dataclass(frozen=True)
class Passed:
    """The passes a rehearsal and a live install share."""

    steps: tuple[CarriedStep, ...]
    read: BusRead


@dataclass(frozen=True)
class Outcome:
    """What one run did to the bus or its copy."""


@dataclass(frozen=True)
class OwnerRestart:
    """An owner queued while its turn ran, and how its restart settled."""

    queued: QueuedRestart
    lines: tuple[str, ...]


@dataclass(frozen=True)
class Rehearsed(Outcome):
    passed: Passed
    copy: CopySize
    restart: OwnerRestart
    check: RuntimeChecked


def rehearse(
    root: Path,
    pending: tuple[Step, ...],
    runtime: Runtime,
    revisions: Revisions,
    check: RuntimeCheck,
) -> Rehearsed:
    """The live install's passes on a copy, with one owner in a turn while they run: it is
    queued to restart onto the new runtime, and restarts only once that turn ends."""
    scratch_base = StorageRule.load(root).scratch_base
    scratch_base.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=scratch_base, prefix="upgrade-bus-rehearsal.") as scratch:
        copy = Path(scratch) / root.name
        size = BusSnapshot.of(root).copy(copy)
        runtime = replace(runtime, home=RehearsalHome.make(Path(scratch), root, copy))
        owner = TurnInProgress.start(runtime, copy, Path(scratch) / "turn")
        try:
            passed = carry_and_read(
                copy,
                pending,
                runtime,
                revisions,
                BeforeStop().all(copy, pending, runtime, revisions),
            )
            restart = owner.restarted(runtime, copy)
        finally:
            owner.stop(runtime, copy)
        return Rehearsed(
            passed,
            size,
            restart,
            replace(check, runtime=runtime).run(copy, Path(scratch), owner.name),
        )


def clock(seconds: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(seconds))


class Turn(ABC):
    """Where the rehearsal's owner is in its turn, as its replies show."""

    @abstractmethod
    def queued_line(self, name: str, at: float) -> str: ...

    @abstractmethod
    def ended(self) -> float:
        """When the turn's reply arrived."""


class TurnRunning(Turn):
    def queued_line(self, name: str, at: float) -> str:
        return f"{name}: queued at {clock(at)}, turn running"

    def ended(self) -> float:
        raise UpgradeRefused("The owner restarted while its turn had not replied")


@dataclass(frozen=True)
class TurnEnded(Turn):
    reply: Message

    def queued_line(self, name: str, at: float) -> str:
        return f"{name}: queued at {clock(at)}, turn already ended"

    def ended(self) -> float:
        return self.reply.timestamp


@dataclass(frozen=True)
class TurnInProgress:
    """A new thread asked for a turn that outlasts the install's passes, so the rehearsal
    restarts an owner in a turn as a live install does."""

    name: str
    token: str
    turn: ClassVar[int] = 240

    @classmethod
    def start(cls, runtime: Runtime, root: Path, project: Path) -> TurnInProgress:
        project.mkdir()
        name, token = f"install-turn-{time.time_ns()}", f"TURN_ENDED_{time.time_ns()}"
        runtime.command(root, "new", "--name", name, "--project", str(project))
        runtime.command(
            root,
            "user-send",
            "--to",
            name,
            "--worktree",
            str(project),
            "--body",
            f"Run `sleep {cls.turn}` in your shell tool, then reply with {token}.",
        )
        return cls(name, token)

    def now(self, runtime: Runtime, root: Path) -> Turn:
        page = runtime.read(
            root,
            DmPage,
            "history",
            "--target",
            f"@{runtime.user(root)}",
            "--as",
            self.name,
            "--limit",
            "20",
        )
        return next(map(TurnEnded, page.carrying(self.name, self.token)), TurnRunning())

    def restarted(self, runtime: Runtime, root: Path, patience: int = 600) -> OwnerRestart:
        """Queue the owner while its turn runs, and its restart once that turn ends."""
        turn = self.now(runtime, root)
        queued, at = runtime.queue_restart(root, self.name), time.time()
        record = runtime.settled(root, queued, patience)
        return OwnerRestart(
            queued,
            (
                turn.queued_line(self.name, at),
                f"{self.name}: turn ended at {clock(self.now(runtime, root).ended())}",
                f"{self.name}: {json.dumps(FieldCodec.encode(record.state))} "
                f"onto {record.target.interpreter}",
            ),
        )

    def stop(self, runtime: Runtime, root: Path) -> None:
        runtime.command(root, "stop", "--name", self.name)


def carry_and_read(
    root: Path,
    pending: tuple[Step, ...],
    runtime: Runtime,
    revisions: Revisions,
    stopping: tuple[CarriedStep, ...],
) -> Passed:
    """The pass a rehearsal and a live install share: each step's files are checked inside
    its own lock, then the bus is read with the new runtime."""
    return Passed(
        (*stopping, *CarryOver().all(root, pending, runtime, revisions)), runtime.verify(root)
    )


@dataclass(frozen=True)
class Publication:
    """What moving to a runtime changes outside the bus. Every link names a path under
    ``current``, so the last replace moves every program and skill at once."""

    runtime: Runtime

    def links(self) -> Mapping[Path, Path]:
        """The runtime's own programs and skills, as its own Core declares them."""
        listed = json.loads(
            self.runtime.report(
                "import json; from pathlib import Path; "
                "from agent_comms.harness_install import installation_links; "
                "print(json.dumps({str(link): str(target) "
                "for link, target in installation_links(Path.home()).items()}))"
            )
        )
        return {Path(link): Path(target) for link, target in listed.items()}

    def publish(self, previous: Runtime) -> None:
        for link, target in self.links().items():
            replace_link(link, CURRENT / target.relative_to(self.runtime.path))
        self.move_extensions(previous)
        replace_link(CURRENT, self.runtime.path)

    def move_extensions(self, previous: Runtime) -> None:
        """Pi loads global extensions by path; one inside the previous runtime's native
        package moves to the same place in this one."""
        path = Path.home() / ".pi/agent/settings.json"
        if not path.exists():
            return
        settings = json.loads(path.read_text())
        old, new = previous.native_package(), self.runtime.native_package()
        extensions = settings.get("extensions", [])
        moved = [
            str(new / Path(entry).relative_to(old)) if Path(entry).is_relative_to(old) else entry
            for entry in extensions
        ]
        if moved != extensions:
            _atomic_write_text(
                path,
                json.dumps({**settings, "extensions": moved}, indent=2) + "\n",
                fsync_parent=True,
            )


def runtime_references(runtimes: list[Path]) -> Mapping[Path, list[int]]:
    """This user's processes naming each runtime in their command, environment, mappings,
    working directory, executable or open files. One that cannot be read names them all."""
    references: dict[Path, list[int]] = {path: [] for path in runtimes}
    for process in Path("/proc").iterdir():
        if not process.name.isdigit() or int(process.name) == os.getpid():
            continue
        try:
            if process.stat().st_uid != os.getuid():
                continue
            contents = [(process / name).read_bytes() for name in ("environ", "cmdline", "maps")]
            contents += [
                os.fsencode(link.readlink()) for link in (process / "cwd", process / "exe")
            ]
            for descriptor in (process / "fd").iterdir():
                with suppress(FileNotFoundError):  # a descriptor may close during the scan
                    contents.append(os.fsencode(descriptor.readlink()))
        except FileNotFoundError:
            continue  # the process exited during the scan
        except PermissionError:
            contents = [os.fsencode(str(path)) for path in runtimes]
        for path, pids in references.items():
            if any(os.fsencode(str(path)) in value for value in contents):
                pids.append(int(process.name))
    return references


@dataclass(frozen=True)
class RuntimeUse:
    runtime: str
    pids: tuple[int, ...]


@dataclass(frozen=True)
class Pruned:
    removed: tuple[str, ...]
    kept: tuple[RuntimeUse, ...]


def prune(keep: set[Path]) -> Pruned:
    """Remove the runtimes no process uses, other than those in ``keep``."""
    candidates = sorted(
        path
        for path in RUNTIMES.iterdir()
        if not path.name.startswith(".") and path.resolve() not in keep
    )
    used = runtime_references(candidates)
    removed = []
    for path in candidates:
        if not used[path] and not runtime_references([path])[path]:
            shutil.rmtree(path)
            removed.append(str(path))
    return Pruned(
        tuple(removed),
        tuple(RuntimeUse(str(path), tuple(pids)) for path, pids in used.items() if pids),
    )


@dataclass(frozen=True)
class Unchanged(Outcome):
    runtime: str


@dataclass(frozen=True)
class Skipped(Outcome):
    """A rehearsal changes nothing on the bus."""


@dataclass(frozen=True)
class Installed(Outcome):
    passed: Passed
    queued: tuple[QueuedRestart, ...]
    services: tuple[str, ...]
    backup: str
    pruned: Pruned


@dataclass(frozen=True)
class Upgrade:
    """One run: the runtimes it moves between, and what it did."""

    previous: str
    runtime: str
    revision: str
    pending: tuple[str, ...]
    rehearsal: Outcome
    live: Outcome = Skipped()


def upgrade(repo: Path, root: Path, target: str, installer: str, rehearse_only: bool) -> Upgrade:
    if not os.access(UV, os.X_OK):
        raise UpgradeRefused(f"No uv executable at {UV}; the installer runs only that uv")
    run("git", "-C", repo, "fetch", "-q", "origin")
    target = run("git", "-C", repo, "rev-parse", "--verify", f"{target}^{{commit}}").strip()
    applied = CarryLedger(root).applied()
    try:
        # A bus that ran steps this revision does not declare is newer than it.
        pending = CarryChain.at(repo, target).pending(applied)
    except ValueError as error:
        raise UpgradeRefused(str(error)) from None
    installed = Runtime.current()
    with tempfile.TemporaryDirectory(dir=CURRENT.parent, prefix=".upgrade-bus.") as scratch:
        revisions = Revisions(repo, Path(scratch))
        try:
            tree = revisions.tree(target)
            runtime = RuntimeBuild(tree).build()
            install = Install.of(
                root, pending, installed, runtime, revisions, installer, RuntimeCheck(runtime, REPO)
            )
            report = Upgrade(
                str(installed.path),
                str(runtime.path),
                target,
                tuple(step.name for step in pending),
                install.rehearsal(),
            )
            return report if rehearse_only else replace(report, live=install.live())
        finally:
            revisions.close()


@dataclass(frozen=True)
class Install(ABC):
    """One run once the target's runtime is built: what it changes on the bus."""

    root: Path
    pending: tuple[Step, ...]
    installed: Runtime
    runtime: Runtime
    revisions: Revisions
    installer: str
    check: RuntimeCheck

    @classmethod
    def of(
        cls,
        root: Path,
        pending: tuple[Step, ...],
        installed: Runtime,
        runtime: Runtime,
        revisions: Revisions,
        installer: str,
        check: RuntimeCheck,
    ) -> Install:
        member = (ChangedInstall, UnchangedInstall)[installed.path == runtime.path and not pending]
        return member(root, pending, installed, runtime, revisions, installer, check)

    @abstractmethod
    def rehearsal(self) -> Outcome: ...

    @abstractmethod
    def live(self) -> Outcome: ...


class ChangedInstall(Install):
    def rehearsal(self) -> Rehearsed:
        return rehearse(self.root, self.pending, self.runtime, self.revisions, self.check)

    def live(self) -> Installed:
        return live(
            self.root, self.pending, self.installed, self.runtime, self.revisions, self.installer
        )


class UnchangedInstall(Install):
    """The current runtime is the one the target builds and no step is pending: nothing
    to rehearse or restart. It only plans the next install."""

    def rehearsal(self) -> Unchanged:
        return Unchanged(str(self.runtime.path))

    def live(self) -> Unchanged:
        PlannedInstallDeadline.after(self.installer, time.time()).plan(self.root)
        return Unchanged(str(self.runtime.path))


def live(
    root: Path,
    pending: tuple[Step, ...],
    installed: Runtime,
    runtime: Runtime,
    revisions: Revisions,
    installer: str,
) -> Installed:
    """The rehearsal's passes on the bus. Owners keep their turns: each restarts onto the
    new runtime when its own turn ends, and the previous runtime stays until none uses it."""
    steps = BeforeStop().all(root, pending, runtime, revisions)
    with held(root), transcripts_held(root):
        backup = Backup.take(
            root,
            ROUTE,
            (root,),
            root.parent / f"{root.name}.upgrade-{time.strftime('%Y%m%dT%H%M%S')}",
        )
    try:
        passed = carry_and_read(root, pending, runtime, revisions, steps)
        Publication(runtime).publish(installed)
        services = HostService.restart_running(runtime.python)
        PlannedInstallDeadline.after(installer, time.time()).plan(root)
    except BaseException as error:
        maintenance = MaintenanceBarrier(RegistryStore.at(root).path)
        state = maintenance.read()
        state.phase.hold_for(maintenance, state, MaintenanceOperator("upgrade-bus"))
        print(
            f"upgrade-bus: stopped after the backup ({type(error).__name__}: {error}); "
            f"maintenance held, bus and {installed.path} preserved; backup: {backup.path}",
            file=sys.stderr,
        )
        raise
    queued = runtime.queue_restarts(root)
    return Installed(
        passed, queued, services, str(backup.path), prune({runtime.path, installed.path})
    )


def stopped(signum: int, frame: object) -> None:
    """A stop signal runs the same abort as a failure; a second one cannot cut that abort short."""
    for each in (signal.SIGTERM, signal.SIGINT):
        signal.signal(each, signal.SIG_IGN)
    raise SystemExit(f"upgrade-bus: stopped by {signal.Signals(signum).name}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--repo",
        type=lambda value: Path(value).expanduser().resolve(),
        default=REPO,
        help="The repository revisions come from (by default, the one holding this file)",
    )
    parser.add_argument(
        "--root",
        type=lambda value: Path(value).expanduser().resolve(),
        default=Path.home() / ".agent-comms",
    )
    parser.add_argument("--to", default="origin/main")
    parser.add_argument(
        "--installer", required=True, help="The unit running this install; the plan names it"
    )
    parser.add_argument(
        "--rehearse",
        action="store_true",
        help="Build the runtime, then carry, read and check a copy of the bus; change nothing",
    )
    args = parser.parse_args()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, stopped)
    try:
        print(
            json.dumps(
                FieldCodec.encode(
                    upgrade(args.repo, args.root, args.to, args.installer, args.rehearse)
                ),
                indent=2,
            )
        )
    except UpgradeRefused as error:
        raise SystemExit(f"upgrade-bus: {error}") from None


if __name__ == "__main__":
    main()
