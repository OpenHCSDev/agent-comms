#!/usr/bin/env python3
"""Upgrade a retained bus and the checkout that runs it, in one command.

    python tools/upgrade_bus.py [--checkout DIR] [--root ~/.agent-comms] [--to origin/main] [--rehearse]

``--checkout`` is the installation to upgrade (by default, the one holding
this file); run the command from the target revision so its steps are known.

The target revision's stack is built in a temporary worktree, relocked to the
latest OpenHCSDev mains, and must import Toad and the ACP client. The bus
ledger (``carries.json``) names the carry steps still pending; each runs from
the revision that declares it (``tools/cutover/carry.py``). Every run first
rehearses on a copy of the bus: the steps, then ``who``, ``threads`` and
``history`` reads, and every file no step names as rewritten must be
byte-identical. The rehearsal prepares native Pi in the staged stack exactly as
the live phase does. ``--rehearse`` stops there.

A live run then stops the running agent owners with the installed CLI, backs
the bus up (tar plus a hash of every file), moves the checkout,
relocks and syncs its stack, prepares native Pi, carries the bus, reads it
back, and starts only the owners it stopped. A failure after the backup
restores the bus, the route, the checkout and its stack, and restarts those
owners. Nothing is sent and no input is replayed: owners start by the normal
start path, which never replays uncertain inputs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Iterator
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

# This file's tree is the target revision: the tool and every step it runs import that
# tree's src, never the src the checkout being upgraded has installed.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cutover.carry import Carried, CarryChain, CarryLedger, RetainedBus, Step, TodoColumnCarry

import agent_comms.activity_checkpoint  # noqa: F401  (declares the activity runtime files)
from agent_comms.active_route import acquire_exclusive_route_lock, route_lock_holders
from agent_comms.coordination_database import CoordinationStore
from agent_comms.external_transcript import ExternalTranscript
from agent_comms.field_codec import FieldCodec
from agent_comms.locked_store import RuntimeFile
from agent_comms.maintenance_barrier import MaintenanceBarrier, MaintenanceOperator, ReadyPhase
from agent_comms.store_files import _store_lock

REPO = Path(__file__).resolve().parents[1]
CLIENT_IMPORTS = "import toad.app, agent_comms.acp"
CONNECTOR = "from agent_comms.connector import main; raise SystemExit(main())"
HOLD_TRANSCRIPTS = """import sys
from contextlib import closing
from agent_comms.coordination_database import CoordinationStore
with closing(CoordinationStore.writer(sys.argv[1], isolation_level=None)) as writer:
    writer.execute("BEGIN IMMEDIATE")
    with open(int(sys.argv[2]), "wb") as held:
        held.write(b"held")
    sys.stdin.read()
    writer.execute("ROLLBACK")
"""
ROUTE = Path.home() / ".local/state/agent-comms/active-route.json"


class UpgradeRefused(Exception):
    """The upgrade stopped before it changed anything it cannot restore."""


def run(*command: str | Path, cwd: Path | None = None, env: dict | None = None) -> str:
    print("+", " ".join(map(str, command)), file=sys.stderr, flush=True)
    completed = subprocess.run([str(part) for part in command], cwd=cwd, env=env,
                               capture_output=True, text=True)
    if completed.returncode:
        raise UpgradeRefused(f"{command[0]} failed: {completed.stderr.strip() or completed.stdout.strip()}")
    return completed.stdout


def bus_files(root: Path) -> dict[str, str]:
    """Every regular file under the bus but its lock files and the runtime files no carry
    writes, by content hash. A SQLite store counts once, under its database's name, by the
    rows SQLite reads from it, so companion files that appear or vanish change nothing."""
    paths = [path for path in sorted(set(root.rglob("*")) - RuntimeFile.paths_in(root))
             if path.is_file() and not path.is_symlink() and not path.name.endswith(".lock")]
    stores = {path for path in paths if CoordinationStore.is_database(path)}
    companions = {companion for store in stores for companion in CoordinationStore.files(store)[1:]}
    return {str(path.relative_to(root)): CoordinationStore.digest(path) if path in stores
            else hashlib.sha256(path.read_bytes()).hexdigest()
            for path in paths if path not in companions}


@contextmanager
def transcripts_held(root: Path) -> Iterator[None]:
    """Bridges publish their CLI's items outside Core's maintenance refusal, so the operator
    holds the transcript store's writer: a bridge's write waits, then fails busy and is
    republished when its watch starts. A carry that needs this store fails the same way in
    the rehearsal, before the backup.

    SQLite's write lock is a POSIX record lock: the whole process owns it, and closing any
    descriptor on the store's files drops it. The backup's tar and ``bus_files`` read those
    files raw, so a separate holder process owns the hold."""
    path = ExternalTranscript.path_in(root)
    if not path.exists():
        yield
        return
    ready, held = os.pipe()
    holder = subprocess.Popen([sys.executable, "-c", HOLD_TRANSCRIPTS, path, str(held)],
                              stdin=subprocess.PIPE, pass_fds=(held,),
                              env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)})
    os.close(held)
    try:
        with open(ready, "rb") as holding:
            if not holding.read():
                raise UpgradeRefused(f"Could not hold the transcript store {path}")
        yield
    finally:
        holder.communicate()


def carried(root: Path, before: dict[str, str], steps: list[dict]) -> dict[str, int]:
    """Only the files a step names as rewritten may differ from ``before``."""
    after = bus_files(root)
    changed = {name for name in before.keys() | after.keys() if before.get(name) != after.get(name)}
    unnamed = sorted(changed - {name for step in steps for name in step["rewritten"]})
    if unnamed:
        raise UpgradeRefused(f"Carries changed files no step names: {unnamed}")
    return {"identical": len(before.keys() - changed), "rewritten": len(changed)}


@dataclass(frozen=True)
class Stack:
    """One checkout's stack environment: the Python and CLI an installation runs."""

    checkout: Path
    home: Path = Path.home()

    @property
    def python(self) -> Path:
        return self.checkout / "stack/.venv/bin/python"

    def relock(self) -> None:
        """OpenHCSDev packages track their mains; the lock only records this resolution."""
        run("uv", "lock", "--project", "stack", "--upgrade", "--python", "3.14", cwd=self.checkout)
        run("uv", "sync", "--project", "stack", "--locked", "--python", "3.14", cwd=self.checkout)
        self.imports()

    def imports(self) -> None:
        run(self.python, "-c", CLIENT_IMPORTS, cwd=self.checkout, env=self.environment())

    def prepare_native(self) -> None:
        run(self.checkout / "stack/bin/prepare-pi-native", cwd=self.checkout)

    def environment(self, *paths: Path) -> dict:
        """``paths`` first; then this checkout's ``src``. ``home`` holds the route its clients read."""
        return {**os.environ, "HOME": str(self.home),
                "PYTHONPATH": os.pathsep.join(map(str, (*paths, self.checkout / "src")))}

    def cli(self, root: Path, *arguments: str) -> dict:
        """One JSON CLI result for this bus alone, never the route's, waiting while the
        route index is still being built."""
        environment = {**self.environment(), "AGENT_COMMS_ROOT": str(root)}
        for _ in range(240):
            completed = subprocess.run(
                [str(self.python), "-c", "from agent_comms.cli import main; main()", "--root", str(root),
                 *arguments], cwd=self.checkout, env=environment, capture_output=True, text=True)
            if "Route index covers" not in completed.stdout:
                break
            time.sleep(5)
        result = json.loads(completed.stdout or "{}")
        if completed.returncode or "error" in result:
            raise UpgradeRefused(f"agent-comms {' '.join(arguments)}: "
                                 f"{completed.stdout.strip() or completed.stderr.strip()}")
        return result

    def verify(self, root: Path) -> dict[str, int]:
        threads = self.cli(root, "threads")["threads"]
        present = self.cli(root, "who")
        history = self.cli(root, "history", "--everything", "--limit", "50")
        return {"threads": len(threads), "who": len(present), "history": len(history.get("messages", ()))}

    def round_trip(self, root: Path) -> dict:
        """The connector Core declares for this bus sends one message and reads it back; it
        needs open maintenance, so it runs last."""
        environment = {**self.environment(), "AGENT_COMMS_ROOT": str(root)}
        return json.loads(run(self.python, "-c", CONNECTOR, "round_trip", "--root", root,
                              cwd=self.checkout, env=environment))


@dataclass
class Revisions:
    """Temporary worktrees of the revisions the pending steps come from."""

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


def carry(step: Step, root: Path, stack: Stack, revisions: Revisions, *flags: str) -> Carried:
    """Run one step from the revision that declares it, with that revision's src and
    ``stack``'s Python: the target's staged stack, never the installed one."""
    if step.columns:
        if "--before-stop" in flags:
            return TodoColumnCarry.before_stop(root, apply=True)
        return TodoColumnCarry.carry_step(
            root, step, CarryChain.at(revisions.repo, step.chain_revision), apply=True)
    tree = revisions.tree(step.revision)
    environment = stack.environment(tree / "src", tree / "tools")
    # Historical conversions use their own source, but their old CLI cannot decide
    # the target chain's order or recognize steps added after that source revision.
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
            CarryChain.at(revisions.repo, step.chain_revision).require_next(
                step.name, CarryLedger(root).applied())
            result = carry(step, root, stack, revisions, "--apply")
            CarryLedger(root).record(step.name)
            result = replace(result, rewritten=(*result.rewritten, CarryLedger.name))
        results.append(result.report(step.name, True))
        RetainedBus.record(revisions.repo, root)
    return results


def copy_bus(root: Path, copy: Path) -> None:
    """Copy under the bus's wire lock so no writer tears the copy; sockets are skipped."""
    def sockets(directory: str, names: list[str]) -> set[str]:
        return {name for name in names
                if stat.S_ISSOCK(os.lstat(os.path.join(directory, name)).st_mode)}

    from agent_comms.store_files import _store_lock

    with _store_lock(root / "wire"):
        shutil.copytree(root, copy, symlinks=True, ignore=sockets)


def rehearsal_home(scratch: Path, root: Path, copy: Path) -> Path:
    """A home whose route is the real one, selecting the copy wherever it selected the bus."""
    home = scratch / "home"
    route = ROUTE.relative_to(Path.home())
    (home / route).parent.mkdir(mode=0o700, parents=True)
    if ROUTE.exists():
        record = json.loads(ROUTE.read_text())
        if Path(record["root"]).resolve() == root.resolve():
            record["root"] = str(copy)
        (home / route).write_text(json.dumps(record))
        (home / route).chmod(0o600)
    return home


def rehearse(root: Path, pending: tuple[Step, ...], stack: Stack, revisions: Revisions) -> dict:
    with tempfile.TemporaryDirectory(dir=root.parent, prefix=f".{root.name}.rehearsal.") as scratch:
        copy = Path(scratch) / root.name
        copy_bus(root, copy)
        stack = replace(stack, home=rehearsal_home(Path(scratch), root, copy))
        stack.prepare_native()
        with transcripts_held(copy):
            before = bus_files(copy)
            steps = [*before_stop(copy, pending, stack, revisions),
                     *carry_all(copy, pending, stack, revisions)]
            files = carried(copy, before, steps)
            read = stack.verify(copy) | stack.round_trip(copy)
        return {"steps": steps, "files": files, "read": read}


@dataclass
class Backup:
    root: Path
    route: Path
    path: Path

    @classmethod
    def take(cls, root: Path, route: Path) -> Backup:
        path = root.parent / f"{root.name}.upgrade-{time.strftime('%Y%m%dT%H%M%S')}"
        path.mkdir(mode=0o700)
        with tarfile.open(path / "bus.tar", "w") as archive:
            archive.add(root, root.name)  # tarfile skips sockets
        (path / "files.json").write_text(json.dumps(bus_files(root), indent=1))
        if route.exists():
            shutil.copy2(route, path / "active-route.json")
        return cls(root, route, path)

    def files(self) -> dict[str, str]:
        return json.loads((self.path / "files.json").read_text())

    def restore(self) -> None:
        """Extract beside the bus, then swap, so a failed extract leaves the bus as it was."""
        staged = Path(tempfile.mkdtemp(dir=self.root.parent, prefix=f".{self.root.name}.restore."))
        with tarfile.open(self.path / "bus.tar") as archive:
            archive.extractall(staged, filter="tar")
        failed = self.root.with_name(f".{self.root.name}.failed-{time.strftime('%Y%m%dT%H%M%S')}")
        with transcripts_held(staged / self.root.name):
            os.rename(self.root, failed)
            os.rename(staged / self.root.name, self.root)
            staged.rmdir()
            if self.files() != bus_files(self.root):
                raise UpgradeRefused(
                    f"The restored bus differs from the backup; the failed bus is at {failed}")
        if (self.path / "active-route.json").exists():
            shutil.copy2(self.path / "active-route.json", self.route)
        print(f"Restored the bus; the failed one is kept at {failed}", file=sys.stderr)


def route_readers(patience: int = 120) -> None:
    """Bus watches close on their own once maintenance is declared. Toad's observation process
    (ui_model/observation.py:1223) and an ACP agent (acp.py:512) hold the route for their whole
    life and never pause, so the wait gives up after ``patience`` seconds and names them."""
    directory = Path.home() / ".local/state/agent-comms"
    if not directory.exists():
        return
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)

    def gave_up(signum: int, frame: object) -> None:
        raise TimeoutError

    previous = signal.signal(signal.SIGALRM, gave_up)
    signal.alarm(patience)
    try:
        acquire_exclusive_route_lock(descriptor)
    except TimeoutError:
        holders = "; ".join(f"PID {holder.pid}: {holder.command}"
                            for holder in route_lock_holders(descriptor))
        raise UpgradeRefused(f"Clients still hold the route after {patience}s: {holders}") from None
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
        os.close(descriptor)


def upgrade(checkout: Path, root: Path, target: str, rehearse_only: bool) -> dict:
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
    if not rehearse_only and not installed.python.exists():
        raise UpgradeRefused(f"No installed stack at {installed.python}")
    with tempfile.TemporaryDirectory(prefix="upgrade-bus.") as scratch:
        revisions = Revisions(checkout, Path(scratch))
        try:
            staged = Stack(revisions.tree(target))
            staged.relock()
            report = {"from": current, "to": target, "pending": [step.name for step in pending],
                      "rehearsal": rehearse(root, pending, staged, revisions)}
            if rehearse_only:
                return report
            report["live"] = live(checkout, root, current, target, pending, installed, staged, revisions)
            return report
        finally:
            revisions.close()


def live(checkout: Path, root: Path, current: str, target: str, pending: tuple[Step, ...],
         installed: Stack, staged: Stack, revisions: Revisions) -> dict:
    """The rehearsal's passes under the rehearsal's stack; ``installed`` only stops, starts
    and reads back its own owners."""
    running = [thread["name"] for thread in installed.cli(root, "threads")["threads"]
               if thread["role"] == "agent" and thread["status"] == "running"]
    before_stop(root, pending, staged, revisions)
    maintenance = MaintenanceBarrier(root / "registry.json")
    held = maintenance.begin(MaintenanceOperator("upgrade-bus"))
    lock = checkout / "stack/uv.lock"
    transcripts = ExitStack()
    try:
        for name in running:
            installed.cli(root, "stop", "--name", name)
        route_readers()
        transcripts.enter_context(transcripts_held(root))
        backup = Backup.take(root, ROUTE)
    except BaseException as error:
        transcripts.close()
        maintenance.clear(held)
        start(installed, root, running)
        print(f"upgrade-bus: aborted before the backup ({type(error).__name__}: {error}); "
              f"maintenance cleared and {running} restarted", file=sys.stderr)
        raise
    shutil.copy2(lock, backup.path / "uv.lock") if lock.exists() else None
    try:
        run("git", "-C", checkout, "merge", "--ff-only", "-q", target)
        installed.relock()
        installed.prepare_native()
        steps = carry_all(root, pending, staged, revisions)
        files = carried(root, backup.files(), steps)
        read = installed.verify(root)
        maintenance.clear(held)
        read |= installed.round_trip(root)
        transcripts.close()
    except BaseException as error:
        if maintenance.read().phase is ReadyPhase:  # the round trip failed on the opened bus
            maintenance.begin(MaintenanceOperator("upgrade-bus"))  # restore brings back ``held``
            route_readers()
        # The failed bus keeps its hold until restore has swapped it out.
        backup.restore()
        transcripts.close()
        RetainedBus.restore(checkout, target)
        run("git", "-C", checkout, "reset", "--keep", "-q", current)
        if (backup.path / "uv.lock").exists():
            shutil.copy2(backup.path / "uv.lock", lock)
        run("uv", "sync", "--project", "stack", "--locked", "--python", "3.14", cwd=checkout)
        installed.prepare_native()
        maintenance.clear(held)
        start(installed, root, running)
        print(f"upgrade-bus: aborted after the backup ({type(error).__name__}: {error}); "
              f"bus restored, maintenance cleared and {running} restarted", file=sys.stderr)
        raise
    start(installed, root, running)
    return {"stopped_and_started": running, "steps": steps, "files": files, "read": read,
            "backup": str(backup.path)}


def start(stack: Stack, root: Path, names: list[str]) -> None:
    for name in names:
        stack.cli(root, "start", "--name", name)


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
    parser.add_argument("--rehearse", action="store_true", help="Carry and read a copy of the bus; change nothing")
    args = parser.parse_args()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, stopped)
    try:
        print(json.dumps(upgrade(args.checkout, args.root, args.to, args.rehearse), indent=2))
    except UpgradeRefused as error:
        raise SystemExit(f"upgrade-bus: {error}")


if __name__ == "__main__":
    main()
