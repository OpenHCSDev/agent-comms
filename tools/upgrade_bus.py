#!/usr/bin/env python3
"""Upgrade a retained bus and the checkout that runs it, in one command.

    python tools/upgrade_bus.py --installer UNIT [--checkout DIR] [--root ~/.agent-comms] [--to origin/main] [--rehearse]

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
keeps maintenance held and preserves the bus, checkout and backup for forward
repair. It never resets live main or replaces a possibly changed bus. Nothing
is sent and no input is replayed: owners start by the normal start path.
"""
from __future__ import annotations

import argparse
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
from dataclasses import dataclass, replace
from pathlib import Path

# This file's tree is the target revision: the tool and every step it runs import that
# tree's src, never the src the checkout being upgraded has installed.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import agent_comms.activity_checkpoint  # noqa: F401  (declares the activity runtime files)
from agent_comms.external_transcript import ExternalTranscript
from agent_comms.child_process import ProcessIdentity
from agent_comms.field_codec import FieldCodec, Id128Text
from agent_comms.history_result import EverythingHistoryPageResult
from agent_comms.host_services import HostService
from agent_comms.maintenance_barrier import HOLDER_NONCE, MaintenanceBarrier, MaintenanceOperator
from agent_comms.registry_store import RegistryStore
from agent_comms.store_files import _store_lock
from agent_comms.todos import PlannedInstallDeadline
from agent_comms.locked_store import RuntimeFile
import agent_comms.restart_queue  # noqa: F401 (declares resettable restart state)
import agent_comms.runtime_info  # noqa: F401 (declares resettable observations)
from cutover.backup import Backup, StoreBackup, UpgradeRefused, bus_files, transcripts_held
from cutover.carry import Carried, CarryChain, CarryLedger, Step, TodoColumnCarry

REPO = Path(__file__).resolve().parents[1]
CLIENT_IMPORTS = "import toad.app, agent_comms.acp"
ROUTE = Path.home() / ".local/state/agent-comms/active-route.json"


def run(*command: str | Path, cwd: Path | None = None, env: dict | None = None) -> str:
    print("+", " ".join(map(str, command)), file=sys.stderr, flush=True)
    completed = subprocess.run([str(part) for part in command], cwd=cwd, env=env,
                               capture_output=True, text=True)
    if completed.returncode:
        raise UpgradeRefused(f"{command[0]} failed: {completed.stderr.strip() or completed.stdout.strip()}")
    return completed.stdout


def carried(root: Path, before: dict[str, str], steps: list[dict]) -> dict[str, int]:
    """Only the files a step names as rewritten may differ from ``before``."""
    selected = before.keys() | {name for step in steps for name in step["rewritten"]}
    after = {name: StoreBackup.digest(root / name) for name in selected
             if (root / name).is_file()}
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

    def relock(self) -> None:
        """OpenHCSDev packages track their mains; the lock only records this resolution."""
        run(self.uv, "lock", "--project", "stack", "--upgrade", "--python", "3.14",
            cwd=self.checkout)
        self.sync()
        self.imports()

    def sync(self) -> None:
        run(self.uv, "sync", "--project", "stack", "--locked", "--python", "3.14",
            cwd=self.checkout)

    def imports(self) -> None:
        run(self.python, "-c", CLIENT_IMPORTS, cwd=self.checkout, env=self.environment())

    def prepare_native(self) -> None:
        run(self.checkout / "stack/bin/prepare-pi-native", cwd=self.checkout)

    def publish(self) -> None:
        """The selected interpreter owns console metadata and harness declarations."""
        run(
            self.python, "-c",
            "from tools.publish_runtime import RuntimePublication; "
            "RuntimePublication.installed().publish_links()",
            cwd=self.checkout, env=self.environment(),
        )

    def environment(self, *paths: Path) -> dict:
        """``paths`` first; then this checkout's ``src``. ``home`` holds the route its clients read."""
        return {**os.environ, "HOME": str(self.home),
                "PYTHONPATH": os.pathsep.join(map(str, (*paths, self.checkout / "src")))}

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

    def owners(self, root: Path, command: str, names: list[str]) -> None:
        """Run the captured owner set together and join every request before proceeding."""
        with ThreadPoolExecutor(max_workers=max(1, len(names))) as pool:
            list(pool.map(lambda name: self.cli(root, command, "--name", name), names))

    def require_alive(self, root: Path, names: list[str], *, seconds: float) -> None:
        """Each replacement must stay alive for the whole interval, not just its launch."""
        selected = {row["name"]: FieldCodec.decode(ProcessIdentity, row["process_identity"])
                    for row in self.cli(root, "threads")["threads"] if row["name"] in names}
        if set(selected) != set(names):
            raise UpgradeRefused("Restart did not return every captured agent")
        deadline = time.monotonic() + seconds
        while True:
            dead = [name for name, process in selected.items() if not process.alive()]
            if dead:
                raise UpgradeRefused(f"Restarted agents exited: {', '.join(dead)}")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.25, remaining))

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
        if run("git", "-C", self.repo, "rev-parse", f"{revision}^{{commit}}").strip() == run(
            "git", "-C", self.repo, "rev-parse", "HEAD"
        ).strip():
            return self.repo
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
    """Run one step from the revision that declares it, with that revision's src and
    ``stack``'s Python: the target's staged stack, never the installed one."""
    if step.columns:
        if "--before-stop" in flags:
            return TodoColumnCarry.before_stop(root, apply="--apply" in flags)
        return TodoColumnCarry.carry_step(
            root, step, CarryChain.at(revisions.repo, step.revision), apply="--apply" in flags)
    tree = revisions.tree(step.written)
    environment = stack.environment(tree / "src", tree / "tools")
    worker = (
        "import importlib,json,sys; from dataclasses import asdict; from pathlib import Path; "
        "step=getattr(importlib.import_module(sys.argv[1]),sys.argv[2]); "
        "print(json.dumps(asdict(getattr(step,sys.argv[3])(Path(sys.argv[4]),apply=sys.argv[5]=='1'))))"
    )
    operation = "before_stop" if "--before-stop" in flags else "carry"
    output = run(stack.python, "-c", worker, f"cutover.{step.name}", step.declaration,
                 operation, root, "1" if "--apply" in flags else "0", cwd=tree, env=environment)
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


def upgrade_files(root: Path, pending: tuple[Step, ...], checkout: Path) -> tuple[Path, ...]:
    """The step declarations and runtime-file family own the entire write/reset set."""
    files = {CarryLedger(root).path}
    files.update(path for step in pending for path in step.current(checkout).backup_files(root))
    files.update(path for path in RuntimeFile.paths_in(root) if path.is_file())
    return tuple(sorted(files))


def copy_bus(root: Path, copy: Path, pending: tuple[Step, ...], checkout: Path) -> None:
    """Copy every step's declared stores, not 25 GiB of unrelated message history.

    Native sessions remain read-only inputs at their original paths. Only the
    declared stores are writable in this pass; the live application is checked
    after installation. SQLite, not raw WAL copying, owns database snapshots.
    """
    held = MaintenanceBarrier(root / "registry.json").witness_paths
    files = {path for path in upgrade_files(root, pending, checkout)
             if path not in held and path.exists() and not path.is_symlink()}
    required = sum(path.stat().st_size for path in files)
    if shutil.disk_usage(copy.parent).free - required < 10 * 1024 ** 3:
        raise UpgradeRefused(
            f"Declared-store copy needs {required} bytes plus 10 GiB free; no agents stopped"
        )
    copy.mkdir(mode=0o700)
    with _store_lock(root / "wire"):
        for source in sorted(files):
            target = copy / source.relative_to(root)
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            StoreBackup.copy_file(source, target)


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
    # Claude Code's transcripts are read-only input that carries date from;
    # the copy reads the real ones.
    (home / ".claude").symlink_to(Path.home() / ".claude")
    return home


def rehearse(root: Path, pending: tuple[Step, ...], stack: Stack, revisions: Revisions) -> dict:
    with tempfile.TemporaryDirectory(
        dir=revisions.scratch, prefix=f".{root.name}.rehearsal."
    ) as scratch:
        copy = Path(scratch) / root.name
        copy_bus(root, copy, pending, stack.checkout)
        reset = RuntimeFile.reset(copy, Path(scratch) / "runtime-before")
        stack = replace(stack, home=rehearsal_home(Path(scratch), root, copy))
        stack.prepare_native()
        # Hold the copy before reading it, exactly as the live run holds the bus.
        os.environ[HOLDER_NONCE] = MaintenanceBarrier(copy / "registry.json").begin(
            MaintenanceOperator.current()).nonce
        with transcripts_held(copy):
            before = bus_files(copy)
        steps, failures = [], []
        # A private copy can keep checking independent steps after a failure.
        # Never stop live agents just to discover the next incompatible store.
        for step in pending:
            try:
                result = carry(step, copy, stack, revisions, "--apply")
                steps.append(result.report(step.name, True))
                CarryLedger(copy).record(step.name)
            except UpgradeRefused as error:
                failures.append({"step": step.name, "error": str(error)})
        if failures:
            raise UpgradeRefused(json.dumps({"rehearsal_failures": failures, "steps": steps}))
        # A second read uses each step's own current-schema checks and must
        # report no further writes. This does not pretend to be a UI check.
        for step in pending:
            try:
                if carry(step, copy, stack, revisions).changed:
                    failures.append({"step": step.name, "error": "second run still changes data"})
            except UpgradeRefused as error:
                failures.append({"step": step.name, "error": str(error)})
        if failures:
            raise UpgradeRefused(json.dumps({"rehearsal_failures": failures, "steps": steps}))
        return {"steps": steps, "files": carried(copy, before, steps),
                "read": {"steps": len(steps)}, "runtime_reset": reset}


def carry_and_read(root: Path, before: dict[str, str], pending: tuple[Step, ...], staged: Stack,
                   reader: Stack, revisions: Revisions, stopping: list[dict]) -> dict:
    """The pass a rehearsal and a live install share once ``before`` was read under the
    transcript hold. The hold is released first: SQLite cannot lend its writer lock to a
    step's process, so a step that writes the transcript store could not run under it."""
    steps = [*stopping, *carry_all(root, pending, staged, revisions)]
    return {"steps": steps, "files": carried(root, before, steps), "read": reader.verify(root)}


def upgrade(checkout: Path, root: Path, target: str, installer: str, rehearse_only: bool) -> dict:
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
            staged = Stack(revisions.tree(target))
            staged.relock()
            install = Install.of(checkout, root, current, target, pending, installed, staged, revisions,
                                 installer)
            report = {"from": current, "to": target, "pending": [step.name for step in pending],
                      "rehearsal": install.rehearsal()}
            if rehearse_only:
                return report
            # This is deliberately after the complete fresh-copy rehearsal.
            # No process is stopped while target steps are still unproved.
            report["live"] = install.live()
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

    @classmethod
    def of(cls, checkout: Path, root: Path, current: str, target: str, pending: tuple[Step, ...],
           installed: Stack, staged: Stack, revisions: Revisions, installer: str) -> Install:
        unchanged = (not pending and current == target
                     and staged.lock.read_bytes() == installed.lock.read_bytes())
        member = (ChangedInstall, UnchangedInstall)[unchanged]
        return member(checkout, root, current, target, pending, installed, staged, revisions, installer)

    @abstractmethod
    def rehearsal(self) -> dict: ...

    @abstractmethod
    def live(self) -> dict: ...


class ChangedInstall(Install):
    def rehearsal(self) -> dict:
        return rehearse(self.root, self.pending, self.staged, self.revisions)

    def live(self) -> dict:
        return live(self.checkout, self.root, self.current, self.target, self.pending,
                    self.installed, self.staged, self.revisions, self.installer)


class UnchangedInstall(Install):
    """The checkout already sits at the target and the relocked lock is the installed one:
    nothing to rehearse, hold or restart. It only plans the next install."""

    def rehearsal(self) -> dict:
        return {"unchanged": self.target}

    def live(self) -> dict:
        self.installed.publish()
        PlannedInstallDeadline.after(self.installer, time.time()).plan(self.root)
        return {"unchanged": self.target}


def live(checkout: Path, root: Path, current: str, target: str, pending: tuple[Step, ...],
         installed: Stack, staged: Stack, revisions: Revisions, installer: str) -> dict:
    """The rehearsal's passes under the rehearsal's stack; ``installed`` only stops, starts
    and reads back its own owners."""
    before_stop(root, pending, staged, revisions)
    running = [thread["name"] for thread in staged.cli(root, "threads")["threads"]
               if thread["role"] == "agent" and thread["status"] == "running"]
    maintenance = MaintenanceBarrier(RegistryStore.at(root).path)
    held = maintenance.begin(MaintenanceOperator.current())
    os.environ[HOLDER_NONCE] = held.nonce  # this run's own CLI calls read under its hold
    lock = installed.lock
    backup = None
    complete = False
    try:
        staged.owners(root, "stop", running)
        backup = StoreBackup.take(root, ROUTE, upgrade_files(root, pending, staged.checkout),
            root.parent / f"{root.name}.upgrade-{time.strftime('%Y%m%dT%H%M%S')}")
        if lock.exists():
            shutil.copy2(lock, backup.path / "uv.lock")
        reset = RuntimeFile.reset(root, backup.path / "runtime-before")
        run("git", "-C", checkout, "merge", "--ff-only", "-q", target)
        Revisions.prepare(checkout)
        installed.relock()
        installed.prepare_native()
        passed = carry_and_read(root, backup.files(), pending, staged, installed, revisions, [])
        installed.publish()
        services = HostService.restart_running(installed.python)
        PlannedInstallDeadline.after(installer, time.time()).plan(root)
        complete = True
        return {"stopped_and_started": running, **passed, "services": services,
                "runtime_reset": reset, "backup": str(backup.path)}
    finally:
        # A failure must restore data AND source before any original can start.
        # A failed restore deliberately exits here with maintenance still held.
        if backup is not None and not complete:
            restored = False
            try:
                backup.restore()
                run("git", "-C", checkout, "switch", "--detach", current)
                saved_lock = backup.path / "uv.lock"
                if saved_lock.exists():
                    shutil.copy2(saved_lock, lock)
                installed.sync()
                restored = True
            finally:
                if not restored:
                    raise UpgradeRefused(
                        f"Restore failed; agents stay stopped and maintenance stays held; "
                        f"backup: {backup.path}"
                    )
        maintenance.clear(held)
        installed.owners(root, "start", running)
        installed.require_alive(root, running, seconds=20)



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
    args = parser.parse_args()
    if not args.rehearse and os.getsid(0) != os.getpid():
        raise SystemExit(detached_live(
            (sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]), args.root,
        ))
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, stopped)
    try:
        print(json.dumps(upgrade(args.checkout, args.root, args.to, args.installer, args.rehearse), indent=2))
    except UpgradeRefused as error:
        raise SystemExit(error.output)


if __name__ == "__main__":
    main()
