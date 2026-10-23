"""The carry family: one chain of idempotent steps whose dry runs say what a bus still needs."""

from agent_comms.bus import Bus

import os
import sqlite3
import subprocess
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest
from tools.debt_ratchet import UncarriedRecordFormat, git

from agent_comms.cli_commands import ArchiveCliCommand
from agent_comms.installer.backup import (
    StoreBackup,
    UpgradeRefused,
    bus_files,
)
from agent_comms.installer.carry import (
    STEPS,
    Carried,
    Carry,
    CarryChain,
    MigrateOperation,
)
from agent_comms.installer.commands import InstallCommands
from agent_comms.installer.practice import PracticeCopy
from agent_comms.installer.upgrade import (
    BackedUpMigration,
    PracticeBus,
    PreparedMigration,
    RehearsalFiles,
    Stack,
    StepRun,
    TargetTree,
    carried,
)
from agent_comms.launch_runtime import LaunchRuntime, RestoredRuntime
from agent_comms.maintenance_barrier import MaintenanceBarrier, MaintenanceOperator
from agent_comms.registry_store import RegistryStore
from agent_comms.thread_execution import ExternalThreadExecution
from agent_comms.thread_status import ArchivedThreadStatus, StoppedThreadStatus
from cutover.carry_commander_project import CommanderProjectCarry
from cutover.carry_drop_connectivity import DropConnectivity
from cutover.carry_parked_threads import ParkedThreads

REPO = Path(__file__).resolve().parents[1]


def step_source() -> str:
    return (
        "from agent_comms.installer.carry import Carried, Carry\n\n"
        "class Step(Carry):\n"
        "    opened_files = ()\n"
        "    def carry(self, *, apply):\n        return Carried(0)\n"
    )


class Repository:
    def __init__(self, path: Path):
        self.path = path
        self.git("init", "-q")
        self.git("config", "user.email", "carry@example.invalid")
        self.git("config", "user.name", "Carry test")

    def git(self, *args: str) -> str:
        return subprocess.check_output(["git", "-C", str(self.path), *args], text=True, timeout=10).strip()

    def commit(self, steps: dict[str, str | None]) -> str:
        """One commit per step, in order, as the main line adds steps one at a time."""
        for name, source in steps.items() or [(None, None)]:
            if name is not None:
                path = self.path / STEPS / f"{name}.py"
                if source is None:
                    path.unlink()
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(source)
            self.git("add", "--all")
            self.git("commit", "-qm", f"step {name}")
        return self.git("rev-parse", "HEAD")


@pytest.mark.parametrize("pending", (False, True))
def test_pending_uses_one_worker_on_a_real_bus(comms, stack, private_directory, pending):
    chain = CarryChain((DropConnectivity, ParkedThreads, CommanderProjectCarry))
    # The worker uses this checkout's actual source and carry declarations.
    tree = TargetTree(REPO, private_directory, "pending")
    tree.path.mkdir()
    (tree.path / "src").symlink_to(REPO / "src", target_is_directory=True)
    (tree.path / "tools").symlink_to(REPO / "tools", target_is_directory=True)
    practice = PracticeBus(private_directory / "practice",
                           PracticeCopy(comms.root, private_directory / "practice" / "bus"),
                           comms.root, private_directory / "spill")
    if pending:
        # ParkedThreads must run before CommanderProjectCarry can decode this.
        (comms.root / "commander.json").write_text('{"parked": []}')
    position = len(chain.steps)
    for index, step in enumerate(chain.steps):
        (answer,) = stack.call_steps(
            (step(comms.root, comms.root, practice.spill_dir),), tree, MigrateOperation(False))
        if answer.changed:
            position = index
            break
    before = len(stack.commands.ran)
    assert practice.pending(chain, stack, tree) == chain.steps[position:]
    commands = stack.commands.ran[before:]
    assert len(commands) == 1
    assert "-m agent_comms.installer.step_worker " in commands[0].command
    assert practice.pending(CarryChain(()), stack, tree) == ()
    assert len(stack.commands.ran) == before + 1
    assert not practice.copy.copied

def record_source(version: int, fields: str) -> str:
    return ("from dataclasses import dataclass\nfrom typing import Annotated, ClassVar\n"
            "from agent_comms.field_codec import Id128Text\n\n"
            "class RootId(Id128Text):\n    \"\"\"Another representation of the same spelling.\"\"\"\n\n"
            f"@dataclass(frozen=True)\nclass Record:\n    version: ClassVar[int] = {version}\n{fields}")


PLAIN = "    root: str\n"
SPELLED = "    root: Annotated[str, Id128Text]\n"


@pytest.mark.refactor_guard
@pytest.mark.parametrize("before, version, fields, step, uncarried", [
    (PLAIN, 1, PLAIN + "    label: str\n", {}, 1),
    (PLAIN, 2, PLAIN + "    label: str\n", {}, 1),
    (PLAIN, 2, PLAIN + "    label: str\n", {"carry_record": step_source()}, 0),
    (PLAIN, 1, PLAIN + "    kind: ClassVar[str] = 'x'\n", {}, 0),
    (SPELLED, 1, "    root: Annotated[str, RootId]\n", {}, 0),
    (PLAIN, 1, SPELLED, {}, 1),
])
def test_a_stored_record_format_change_bumps_its_version_and_ships_a_carry(
        tmp_path, before, version, fields, step, uncarried):
    repo = Repository(tmp_path)
    package = git(REPO, "archive", "HEAD", "--", "src/agent_comms")
    subprocess.run(["tar", "-x", "-C", str(tmp_path)], input=package, check=True, timeout=60)
    record = tmp_path / "src" / "agent_comms" / "record.py"
    record.write_text(record_source(1, before))
    base = repo.commit({})
    record.write_text(record_source(version, fields))
    head = repo.commit(step)
    changed = {"src/agent_comms/record.py"}
    assert UncarriedRecordFormat.compare(tmp_path, base, head, changed, "src/agent_comms") == (
        {"UncarriedRecordFormat": 0}, {"UncarriedRecordFormat": uncarried})


def test_a_store_compares_by_its_rows_and_not_by_its_companion_files(tmp_path):
    store = tmp_path / "todos.sqlite3"
    with closing(sqlite3.connect(store)) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE todo (id)")
        db.execute("INSERT INTO todo VALUES (1)")
        db.commit()
    before = bus_files(tmp_path)
    with closing(sqlite3.connect(store, autocommit=True)) as opened:
        opened.execute("BEGIN IMMEDIATE")
        opened.execute("ROLLBACK")
        assert {"todos.sqlite3-wal", "todos.sqlite3-shm"} <= {path.name for path in tmp_path.iterdir()}
        steps = (StepRun("carry_nothing", Carried(0)),)
        assert carried(tmp_path, before, steps, RehearsalFiles.declared(steps)) == (
            RehearsalFiles(1, 0, (), 0))
        opened.execute("UPDATE todo SET id = 2")
        with pytest.raises(UpgradeRefused):
            carried(tmp_path, before, steps, RehearsalFiles.declared(steps))


@pytest.fixture
def stack(tmp_path):
    """CLI children keep the test's source path rather than an editable install's checkout."""
    return Stack(REPO, InstallCommands(), home=tmp_path,
                 runtime=RestoredRuntime(LaunchRuntime.running().interpreter))


@pytest.fixture(scope="module")
def noisy_step(tmp_path_factory):
    tree = TargetTree(REPO, tmp_path_factory.mktemp("noisy-step"), "noisy-step")
    (tree.path / "tools" / "cutover").mkdir(parents=True)
    (tree.path / "src").symlink_to(REPO / "src", target_is_directory=True)
    module = tree.path / "tools" / "cutover" / "carry_noisy.py"
    module.write_text(
        "from pathlib import Path\n"
        "from agent_comms.installer.carry import Carry, Carried\n"
        "class Noisy(Carry):\n"
        "    opened_files = ()\n"
        "    def carry(self, *, apply):\n"
        "        import atexit\n"
        "        atexit.register(print, 'diagnostic after the answer')\n"
        "        if apply:\n"
        "            (self.root / 'saved').write_text('migrated')\n"
        "            if (self.root / 'fail').exists():\n"
        "                raise RuntimeError('failed after changing the saved file')\n"
        "        return Carried(1, {'applied': apply}, (Path('saved'),))\n"
    )
    # The parent needs only the selected step's identity; its implementation is loaded
    # by the worker from the target tree.
    class Noisy(Carry):
        @classmethod
        def module(cls):
            return module

        opened_files = ()

        def carry(self, *, apply):
            raise AssertionError("The selected interpreter executes the carry, never its caller")

    return tree, Noisy


def test_step_answers_are_separate_from_diagnostics_and_keep_their_types(
        stack, tmp_path, noisy_step):
    """Every invocation crosses the real worker process, including a noisy migration."""
    tree, step = noisy_step
    root = tmp_path / "bus"
    root.mkdir()
    request = step(root, root, tmp_path / "spill")
    assert stack.call_steps((request,), tree, MigrateOperation(False)) == (
        Carried(1, {"applied": False}, (Path("saved"),)),)
    assert not (root / "saved").exists()
    assert stack.call_steps((request,), tree, MigrateOperation(True)) == (
        Carried(1, {"applied": True}, (Path("saved"),)),)
    assert (root / "saved").read_text() == "migrated"


def test_backed_up_migration_restores_a_real_worker_failure(comms, stack, noisy_step):
    tree, step = noisy_step
    saved = comms.root / "saved"
    saved.write_text("original")
    (comms.root / "fail").touch()
    maintenance = MaintenanceBarrier(RegistryStore.at(comms.root).path)
    hold = maintenance.begin(MaintenanceOperator.current())
    with hold.holding():
        migration = PreparedMigration.capture(hold, stack).extended(
            BackedUpMigration, backup=StoreBackup.take(
                comms.root, comms.root / "no-route", (saved,), comms.root.parent / "spill"))
        with pytest.raises(UpgradeRefused, match="failed after changing the saved file"):
            migration.carry((step(comms.root, comms.root, comms.root.parent / "spill"),), stack, tree)
        assert saved.read_text() == "migrated"
        migration.restore()
        assert saved.read_text() == "original"
        assert stack.verify(comms.root) == migration.read
    maintenance.clear(hold)


def test_restart_selection_uses_receiving_declaration_with_connected_cli_inbox(comms, stack):
    from agent_comms.child_process import ProcessIdentity

    process = ProcessIdentity.capture(os.getpid())
    native = comms.registry.declare(replace(comms.registry.new_thread(
        'native-copy', frozenset(), str(comms.root)), process_identity=process))
    external = comms.registry.declare(comms.registry.new_thread(
        'cli-inbox-copy', frozenset(), str(comms.root), execution=ExternalThreadExecution))
    with comms.registry.external_process(external.name, process):
        assert stack.running_owners(comms.root) == [native.name]
        native.execution.require_native()
        with pytest.raises(ValueError):
            external.execution.require_native()


def test_owner_request_failure_joins_the_entire_set_before_propagating(comms, stack):
    Bus.install(Bus.path_in(comms.root))
    names = ["failed", "other", "last"]
    for name in names[1:]:
        comms.registry.declare(
            comms.registry.new_thread(name, frozenset(), str(comms.root)), StoppedThreadStatus()
        )

    # The first real CLI request fails; both other children must finish their writes
    # before owners propagates that failure. No replacement Stack.cli is involved.
    with pytest.raises(UpgradeRefused, match="failed"):
        stack.owners(comms.root, ArchiveCliCommand, names)
    saved = comms.registry.snapshot()
    assert tuple(saved.status(name) for name in names[1:]) == (ArchivedThreadStatus(),) * 2


def test_upgrade_refuses_at_start_without_its_own_uv(stack):
    """The installer names its own uv, even when no installed runtime exists."""
    assert not stack.uv.exists()
    with pytest.raises(UpgradeRefused):
        stack.require_uv()

