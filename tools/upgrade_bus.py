#!/usr/bin/env python3
"""Install a target revision of agent-comms on a retained bus, in one command.

    python tools/upgrade_bus.py --installer UNIT [--repo DIR] [--root ~/.agent-comms] [--to origin/main] [--rehearse]

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
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager, suppress
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import ClassVar

# This file's tree is the target revision: the tool and every step it runs import that
# tree's src, never the src of the runtime being replaced.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cutover.backup import Backup, UpgradeRefused, bus_files, transcripts_held
from cutover.carry import Carried, CarryChain, CarryLedger, Step, TodoColumnCarry

import agent_comms.activity_checkpoint  # noqa: F401  (declares the activity runtime files)
import agent_comms.cli  # noqa: F401  (imports every owner, so each declared bus location is known)
from agent_comms.coordination_database import CoordinationStore
from agent_comms.field_codec import FieldCodec
from agent_comms.harness_install import replace_link
from agent_comms.history_result import EverythingHistoryPageResult
from agent_comms.host_services import HostService
from agent_comms.locked_store import HistoryDirectory
from agent_comms.maintenance_barrier import MaintenanceBarrier, MaintenanceOperator, ReadyPhase
from agent_comms.native_arguments import NativeArguments
from agent_comms.owner_cutover import StoppedOwnerInstallation
from agent_comms.registry_store import RegistryStore
from agent_comms.storage_rules import StorageRule
from agent_comms.store_files import _atomic_write_text, _store_lock
from agent_comms.todos import PlannedInstallDeadline
from agent_comms.wire_log import WireLog

REPO = Path(__file__).resolve().parents[1]
CLIENT_IMPORTS = "import toad.app, agent_comms.acp"
ROUTE = Path.home() / ".local/state/agent-comms/active-route.json"
PI = Path.home() / ".pi/agent"
CURRENT = StoppedOwnerInstallation.current_pointer()
RUNTIMES = CURRENT.parent / "runtimes"
UV = Path.home() / ".local/state/agent-comms/upgrade-bus/uv"
"""The uv the installer runs, kept in the installer's own state."""


def run(*command: str | Path, cwd: Path | None = None, env: dict | None = None) -> str:
    print("+", " ".join(map(str, command)), file=sys.stderr, flush=True)
    completed = subprocess.run([str(part) for part in command], cwd=cwd, env=env,
                               capture_output=True, text=True)
    if completed.returncode:
        raise UpgradeRefused(f"{command[0]} failed: {completed.stderr.strip() or completed.stdout.strip()}")
    return completed.stdout


def carried(root: Path, before: dict[str, str], steps: list[dict]) -> dict[str, int]:
    """Only the files a step names as rewritten may differ from ``before``."""
    after = bus_files(root)
    changed = {name for name in before.keys() | after.keys() if before.get(name) != after.get(name)}
    unnamed = sorted(changed - {name for step in steps for name in step["rewritten"]})
    if unnamed:
        raise UpgradeRefused(f"Carries changed files no step names: {unnamed}")
    return {"identical": len(before.keys() - changed), "rewritten": len(changed)}


def stepped(root: Path, step: Step, apply: Callable[[], Carried]) -> dict:
    """One step under the wire lock, which every writer of the bus takes, so the files it
    changes are the files it names even while owners run. The transcript store's writers
    take SQLite's lock instead: it is held while ``before`` is read, never across the step,
    which may write that store itself."""
    with _store_lock(root / "wire"):
        with transcripts_held(root):
            before = bus_files(root)
        report = apply().report(step.name, True)
        return {**report, "files": carried(root, before, [report])}


@dataclass(frozen=True)
class Home:
    """The home a runtime's clients read their route and settings from."""

    path: Path = Path.home()

    def environment(self, inherited: dict[str, str]) -> dict[str, str]:
        return {**inherited, "HOME": str(self.path)}


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
        if ROUTE.exists():
            record = json.loads(ROUTE.read_text())
            if Path(record["root"]).resolve() == root.resolve():
                record["root"] = str(copy)
            route.write_text(json.dumps(record))
            route.chmod(0o600)
        pi = scratch / "pi"
        pi.mkdir(mode=0o700)
        settings = PI / "settings.json"
        if settings.exists():
            shutil.copy2(settings, pi / "settings.json")
        return cls(home, pi)

    def environment(self, inherited: dict[str, str]) -> dict[str, str]:
        environment = {name: value for name, value in super().environment(inherited).items()
                       if name not in ("AGENT_COMMS_ROOT", "AGENT_COMMS_THREAD", "DISPLAY")}
        return {**environment, "AGENT_COMMS_NATIVE_CONFIG_DIR": str(PI),
                "PI_CODING_AGENT_DIR": str(self.pi)}


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

    def environment(self, *paths: Path) -> dict:
        """``paths`` alone are importable beside the runtime's own packages; ``home``
        holds the route its clients read."""
        environment = {**self.home.environment(dict(os.environ)),
                       NativeArguments.binary_key: str(self.python.with_name("pi-comms-native"))}
        environment.pop("PYTHONPATH", None)
        if paths:
            environment["PYTHONPATH"] = os.pathsep.join(map(str, paths))
        return environment

    def report(self, code: str, *arguments: str | Path) -> str:
        """One answer the runtime gives about itself, from its own packages only."""
        return run(self.python, "-I", "-c", code, *arguments, env=self.environment()).strip()

    def native_package(self) -> Path:
        return Path(self.report("from agent_comms.native_package import pinned_location; "
                                "print(pinned_location())"))

    def cli(self, root: Path, *arguments: str) -> dict:
        """One JSON CLI result for this bus alone, never the route's, waiting while the
        route index is still being built."""
        for _ in range(240):
            completed = subprocess.run(
                [str(self.python), "-c", "from agent_comms.cli import main; main()", "--root", str(root),
                 *arguments], cwd=self.path, env=self.environment(), capture_output=True, text=True)
            if "Route index covers" not in completed.stdout:
                break
            time.sleep(5)
        result = json.loads(completed.stdout or "{}")
        if completed.returncode or "error" in result:
            raise UpgradeRefused(f"agent-comms {' '.join(arguments)}: "
                                 f"{completed.stdout.strip() or completed.stderr.strip()}")
        return result

    def running(self, root: Path) -> list[str]:
        return [thread["name"] for thread in self.cli(root, "threads")["threads"]
                if thread["role"] == "agent" and thread["status"] == "running"]

    def queue_restart(self, root: Path, name: str) -> dict:
        """Restart this owner onto this runtime when its own turn ends; never interrupts it."""
        return self.cli(root, "tool", "--tool", "comms_queue_restart",
                        "--arguments", json.dumps({"name": name}))

    def settled(self, root: Path, queued: dict, patience: int) -> dict:
        """The queued restart once it settles; a settled restart names the owner's
        previous process and the one that replaced it."""
        return json.loads(run(self.python, "-I", Path(__file__).with_name("restart_settled.py"),
                              root, queued["restart"]["id"], str(patience), env=self.environment()))

    def scratch_base(self, root: Path) -> Path:
        """Where this bus's clients keep scratch, as this runtime's home reads it."""
        return Path(self.report("import sys; from pathlib import Path; "
                                "from agent_comms.storage_rules import StorageRule; "
                                "print(StorageRule.load(Path(sys.argv[1])).scratch_base)", root))

    def user(self, root: Path) -> str:
        return self.report(
            "import sys; from pathlib import Path; from agent_comms.comms import wire; "
            "print(wire(Path(sys.argv[1])).registry.user_thread()"
            ".require(RuntimeError('the bus has no user')).name)", root)

    def verify(self, root: Path) -> dict[str, int]:
        threads = self.cli(root, "threads")["threads"]
        present = self.cli(root, "who")
        history = self.cli(root, "history", "--limit", "50")  # #any: every channel
        return {"threads": len(threads), "who": len(present["who"]),
                "history": len(history[EverythingHistoryPageResult.messages_key])}


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
        return run(UV, "export", "--project", "stack", "--frozen", "--no-dev", "--no-header",
                   "--no-annotate", "--no-emit-project", "--no-emit-package", "agent-comms",
                   cwd=self.tree)

    def build(self) -> Runtime:
        requirements = self.requirements()
        commit = run("git", "-C", self.tree, "rev-parse", "HEAD").strip()
        pin = hashlib.sha256((self.tree / "stack/pi-native.sha256").read_bytes()).hexdigest()
        dependencies = hashlib.sha256(requirements.encode()).hexdigest()
        runtime = Runtime(self.runtimes / f"core-{commit[:9]}-deps-{dependencies[:12]}-native-{pin[:16]}")
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
            run(UV, "pip", "install", "--python", runtime.python, "--no-deps",
                "-r", wheels / "requirements.txt", wheel)
        package = Path(runtime.report("import agent_comms; print(agent_comms.__path__[0])"))
        if (package / "package-lock.json").is_file():
            run("npm", "ci", "--ignore-scripts", "--omit=dev", "--no-audit", "--no-fund", cwd=package)
        run(UV, "pip", "check", "--python", runtime.python)
        self.native(runtime)
        run(runtime.path / "bin/toad", "--version")
        runtime.report(CLIENT_IMPORTS)
        record = {"core_commit": commit, "native_manifest_sha256": pin,
                  "requirements": requirements, "packages": json.loads(runtime.report(INVENTORY))}
        _atomic_write_text(runtime.record, json.dumps(record, indent=2) + "\n", fsync_parent=True)
        return runtime

    def native(self, runtime: Runtime) -> None:
        """The revision's own preparation builds native Pi; the runtime keeps the result."""
        run(self.tree / "stack/bin/prepare-pi-native", "--python", runtime.python, cwd=self.tree)
        built = runtime.native_package().parents[2]
        built.parent.mkdir(parents=True, exist_ok=True)
        os.rename(self.tree / "stack" / built.name, built)
        runtime.report("from agent_comms.native_package import pinned_native_package, "
                       "verify_native_package; verify_native_package(pinned_native_package())")


@dataclass(frozen=True)
class ToadSource:
    """The Toad commit a runtime installed, fetched for the checks Toad keeps in its tools."""

    url: str
    commit: str

    @classmethod
    def of(cls, runtime: Runtime) -> ToadSource:
        source = json.loads(runtime.record.read_text())["packages"]["batrachian-toad"]["source"]
        return cls(source["url"], source["vcs_info"]["commit_id"])

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

    def run(self, copy: Path, scratch: Path, thread: str) -> dict:
        toad = ToadSource.of(self.runtime).fetch(scratch / "toad")
        output = scratch / "check"
        into = self.runtime.scratch_base(copy) / "install-check" / scratch.name
        run(self.runtime.python, "-c", "from agent_comms.cli import main; main()", "--root", copy,
            "check-root", "--threads", thread, "--into", into, "--",
            self.runtime.python, "-I", self.tree / "tools/runtime_check.py",
            "--root", "{root}", "--toad", toad, "--output", output,
            cwd=scratch, env=self.runtime.environment())
        return json.loads((output / "result.json").read_text())


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
            subprocess.run(["git", "-C", str(self.repo), "worktree", "remove", "--force", str(path)])


def carry(step: Step, root: Path, runtime: Runtime, revisions: Revisions, *flags: str) -> Carried:
    """Run one step from the revision that declares it, with that revision's src and the
    new runtime's Python, never the one being replaced."""
    if step.columns:
        if "--before-stop" in flags:
            return TodoColumnCarry.before_stop(root, apply=True)
        return TodoColumnCarry.carry_step(
            root, step, CarryChain.at(revisions.repo, step.revision), apply=True)
    tree = revisions.tree(step.revision)
    environment = runtime.environment(tree / "src", tree / "tools")
    worker = (
        "import importlib,json,sys; from dataclasses import asdict; from pathlib import Path; "
        "step=getattr(importlib.import_module(sys.argv[1]),sys.argv[2]); "
        "print(json.dumps(asdict(getattr(step,sys.argv[3])(Path(sys.argv[4]),apply=True))))"
    )
    operation = "before_stop" if "--before-stop" in flags else "carry"
    output = run(runtime.python, "-c", worker, f"cutover.{step.name}", step.declaration,
                 operation, root, cwd=tree, env=environment)
    return FieldCodec.decode(Carried, FieldCodec.parse(output.strip().splitlines()[-1]))


def before_stop(
    root: Path, pending: tuple[Step, ...], runtime: Runtime, revisions: Revisions
) -> list[dict]:
    """What the running installation needs before its owners stop and read the bus."""
    return [stepped(root, step, partial(carry, step, root, runtime, revisions, "--before-stop",
                                        "--apply"))
            for step in pending]


def carry_all(
    root: Path, pending: tuple[Step, ...], runtime: Runtime, revisions: Revisions
) -> list[dict]:
    return [stepped(root, step, partial(CarryLedger(root).apply,
                                        CarryChain.at(revisions.repo, step.revision), step.name,
                                        partial(carry, step, root, runtime, revisions, "--apply")))
            for step in pending]


@contextmanager
def held(root: Path) -> Iterator[None]:
    """No writer changes the bus while its files are read whole: every writer takes the
    wire lock, and a reader repairing the log's checkpoint takes the log's lock."""
    with _store_lock(root / "wire"), WireLog.at(root).locked():
        yield


def snapshot_store(source: Path, target: Path) -> None:
    """One consistent database, read in a single transaction by sqlite's backup: units
    keep writing their databases outside the wire lock, so their files are never copied."""
    with closing(CoordinationStore.read_snapshot(source)) as database, \
            closing(sqlite3.connect(target)) as snapshot:
        database.backup(snapshot)
    shutil.copystat(source, target)


def copied(copy: Path) -> int:
    """Bytes the copy holds on disk."""
    return sum(path.lstat().st_blocks * 512 for path in copy.rglob("*"))


def copy_bus(root: Path, copy: Path) -> None:
    """A consistent snapshot of a bus its units keep writing. The log and every file a
    writer changes under the wire lock are copied holding the wire and log locks; each
    database is read whole by sqlite's backup, and its write-ahead files are not copied.
    Only files, directories and links are copied, so sockets and names already gone are
    skipped, and no history directory: starting the bus and one fresh turn never open
    one. An open barrier's witness names the original root, and a copy holding it
    would read as inconsistent, so an open copy starts as a bus that never held maintenance."""
    witness = MaintenanceBarrier(RegistryStore.at(root).path)
    history = HistoryDirectory.paths_in(root)

    def kind(directory: str, name: str) -> int:
        # Presence files are replaced outside the wire lock; a name renamed away
        # between listing and copying is not part of the bus.
        try:
            return os.lstat(os.path.join(directory, name)).st_mode
        except FileNotFoundError:
            return 0

    def store(path: Path) -> bool:
        try:
            return CoordinationStore.is_database(path)
        except FileNotFoundError:
            return False

    def skipped(directory: str, names: list[str]) -> set[str]:
        modes = {name: kind(directory, name) for name in names}
        skip = {name for name, mode in modes.items() if not stat.S_ISREG(mode)
                and not stat.S_ISDIR(mode) and not stat.S_ISLNK(mode)}
        skip |= {companion.name for name, mode in modes.items()
                 if stat.S_ISREG(mode) and store(path := Path(directory, name))
                 for companion in CoordinationStore.files(path)[1:]}
        if Path(directory) == root:
            skip |= {path.name for path in (*witness.witness_paths, *history)}
        return skip

    def present(source: str, target: str) -> None:
        with suppress(FileNotFoundError):
            (snapshot_store if store(Path(source)) else shutil.copy2)(Path(source), Path(target))

    with held(root):
        if witness.current_unlocked().phase is not ReadyPhase:
            raise UpgradeRefused("Maintenance holds the bus; finish or repair that first")
        shutil.copytree(root, copy, symlinks=True, ignore=skipped, copy_function=present)


def rehearse(root: Path, pending: tuple[Step, ...], runtime: Runtime, revisions: Revisions,
             check: RuntimeCheck) -> dict:
    """The live install's passes on a copy, with one owner in a turn while they run: it is
    queued to restart onto the new runtime, and restarts only once that turn ends."""
    scratch_base = StorageRule.load(root).scratch_base
    scratch_base.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=scratch_base, prefix="upgrade-bus-rehearsal.") as scratch:
        copy = Path(scratch) / root.name
        copy_bus(root, copy)
        home = RehearsalHome.make(Path(scratch), root, copy)
        runtime = replace(runtime, home=home)
        owner = TurnInProgress.start(runtime, copy, Path(scratch) / "turn")
        try:
            steps = before_stop(copy, pending, runtime, revisions)
            passed = carry_and_read(copy, pending, runtime, revisions, steps)
            queued, restarted = owner.restarted(runtime, copy)
        finally:
            owner.stop(runtime, copy)
        return {**passed, "copied": f"{copied(copy) / 2**20:.0f} MiB",
                "queued": {owner.name: queued}, "restarted": restarted,
                "check": replace(check, runtime=runtime).run(copy, Path(scratch), owner.name)}


def clock(seconds: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(seconds))


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
        runtime.cli(root, "new", "--name", name, "--project", str(project))
        runtime.cli(root, "user-send", "--to", name, "--worktree", str(project), "--body",
                    f"Run `sleep {cls.turn}` in your shell tool, then reply with {token}.")
        return cls(name, token)

    def reply(self, runtime: Runtime, root: Path) -> dict | None:
        return next((message for message in runtime.cli(
            root, "history", "--target", f"@{runtime.user(root)}", "--as", self.name, "--limit", "20")["dm"]
            if message["from"] == self.name and self.token in message["text"]), None)

    def restarted(self, runtime: Runtime, root: Path, patience: int = 600) -> tuple[dict, list[str]]:
        """Queue the owner while its turn runs, and its restart once that turn ends."""
        running = self.reply(runtime, root) is None
        queued, at = runtime.queue_restart(root, self.name), time.time()
        record = runtime.settled(root, queued, patience)
        ended = self.reply(runtime, root)
        return queued, [
            f"{self.name}: queued at {clock(at)}, turn {'running' if running else 'already ended'}",
            f"{self.name}: turn ended at {clock(ended['ts'])}",
            f"{self.name}: {json.dumps(record['state'])} onto {record['target']['interpreter']}"]

    def stop(self, runtime: Runtime, root: Path) -> None:
        runtime.cli(root, "stop", "--name", self.name)


def carry_and_read(root: Path, pending: tuple[Step, ...], runtime: Runtime, revisions: Revisions,
                   stopping: list[dict]) -> dict:
    """The pass a rehearsal and a live install share: each step's files are checked inside
    its own lock, then the bus is read with the new runtime."""
    return {"steps": [*stopping, *carry_all(root, pending, runtime, revisions)],
            "read": runtime.verify(root)}


@dataclass(frozen=True)
class Publication:
    """What moving to a runtime changes outside the bus. Every link names a path under
    ``current``, so the last replace moves every program and skill at once."""

    runtime: Runtime

    def links(self) -> dict[Path, Path]:
        """The runtime's own programs and skills, as its own Core declares them."""
        listed = json.loads(self.runtime.report(
            "import json; from pathlib import Path; "
            "from agent_comms.harness_install import installation_links; "
            "print(json.dumps({str(link): str(target) "
            "for link, target in installation_links(Path.home()).items()}))"))
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
        moved = [str(new / Path(entry).relative_to(old)) if Path(entry).is_relative_to(old) else entry
                 for entry in extensions]
        if moved != extensions:
            _atomic_write_text(path, json.dumps({**settings, "extensions": moved}, indent=2) + "\n",
                               fsync_parent=True)


def runtime_references(runtimes: list[Path]) -> dict[Path, list[int]]:
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
            contents += [os.fsencode(link.readlink()) for link in (process / "cwd", process / "exe")]
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


def prune(keep: set[Path]) -> dict:
    """Remove the runtimes no process uses, other than those in ``keep``."""
    candidates = sorted(path for path in RUNTIMES.iterdir()
                        if not path.name.startswith(".") and path.resolve() not in keep)
    used = runtime_references(candidates)
    removed = []
    for path in candidates:
        if not used[path] and not runtime_references([path])[path]:
            shutil.rmtree(path)
            removed.append(str(path))
    return {"removed": removed, "kept_by_pid": {str(path): pids for path, pids in used.items() if pids}}


def upgrade(repo: Path, root: Path, target: str, installer: str, rehearse_only: bool) -> dict:
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
            install = Install.of(root, pending, installed, runtime, revisions, installer,
                                 RuntimeCheck(runtime, tree))
            report = {"from": str(installed.path), "to": str(runtime.path), "revision": target,
                      "pending": [step.name for step in pending], "rehearsal": install.rehearsal()}
            if rehearse_only:
                return report
            report["live"] = install.live()
            return report
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
    def of(cls, root: Path, pending: tuple[Step, ...], installed: Runtime, runtime: Runtime,
           revisions: Revisions, installer: str, check: RuntimeCheck) -> Install:
        member = (ChangedInstall, UnchangedInstall)[installed.path == runtime.path and not pending]
        return member(root, pending, installed, runtime, revisions, installer, check)

    @abstractmethod
    def rehearsal(self) -> dict: ...

    @abstractmethod
    def live(self) -> dict: ...


class ChangedInstall(Install):
    def rehearsal(self) -> dict:
        return rehearse(self.root, self.pending, self.runtime, self.revisions, self.check)

    def live(self) -> dict:
        return live(self.root, self.pending, self.installed, self.runtime, self.revisions,
                    self.installer)


class UnchangedInstall(Install):
    """The current runtime is the one the target builds and no step is pending: nothing
    to rehearse or restart. It only plans the next install."""

    def rehearsal(self) -> dict:
        return {"unchanged": str(self.runtime.path)}

    def live(self) -> dict:
        PlannedInstallDeadline.after(self.installer, time.time()).plan(self.root)
        return {"unchanged": str(self.runtime.path)}


def live(root: Path, pending: tuple[Step, ...], installed: Runtime, runtime: Runtime,
         revisions: Revisions, installer: str) -> dict:
    """The rehearsal's passes on the bus. Owners keep their turns: each restarts onto the
    new runtime when its own turn ends, and the previous runtime stays until none uses it."""
    steps = before_stop(root, pending, runtime, revisions)
    with held(root), transcripts_held(root):
        backup = Backup.take(root, ROUTE, (root,),
                             root.parent / f"{root.name}.upgrade-{time.strftime('%Y%m%dT%H%M%S')}")
    try:
        passed = carry_and_read(root, pending, runtime, revisions, steps)
        Publication(runtime).publish(installed)
        services = HostService.restart_running(runtime.python)
        PlannedInstallDeadline.after(installer, time.time()).plan(root)
    except BaseException as error:
        maintenance = MaintenanceBarrier(RegistryStore.at(root).path)
        state = maintenance.read()
        state.phase.hold_for(maintenance, state, MaintenanceOperator("upgrade-bus"))
        print(f"upgrade-bus: stopped after the backup ({type(error).__name__}: {error}); "
              f"maintenance held, bus and {installed.path} preserved; backup: {backup.path}",
              file=sys.stderr)
        raise
    queued = {name: runtime.queue_restart(root, name) for name in installed.running(root)}
    return {**passed, "queued": queued, "services": services, "backup": str(backup.path),
            "pruned": prune({runtime.path, installed.path})}


def stopped(signum: int, frame: object) -> None:
    """A stop signal runs the same abort as a failure; a second one cannot cut that abort short."""
    for each in (signal.SIGTERM, signal.SIGINT):
        signal.signal(each, signal.SIG_IGN)
    raise SystemExit(f"upgrade-bus: stopped by {signal.Signals(signum).name}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", type=lambda value: Path(value).expanduser().resolve(), default=REPO,
                        help="The repository revisions come from (by default, the one holding this file)")
    parser.add_argument("--root", type=lambda value: Path(value).expanduser().resolve(),
                        default=Path.home() / ".agent-comms")
    parser.add_argument("--to", default="origin/main")
    parser.add_argument("--installer", required=True,
                        help="The unit running this install; the plan names it")
    parser.add_argument("--rehearse", action="store_true",
                        help="Build the runtime, then carry, read and check a copy of the bus; change nothing")
    args = parser.parse_args()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, stopped)
    try:
        print(json.dumps(upgrade(args.repo, args.root, args.to, args.installer, args.rehearse), indent=2))
    except UpgradeRefused as error:
        raise SystemExit(f"upgrade-bus: {error}")


if __name__ == "__main__":
    main()
