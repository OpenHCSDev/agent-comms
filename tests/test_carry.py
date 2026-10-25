"""The carry family: one chain of idempotent steps whose dry runs say what a bus still needs."""


import os
import sqlite3
import subprocess
from contextlib import closing
from dataclasses import replace
from functools import partial
from pathlib import Path

import pytest
from debt_ratchet import UncarriedRecordFormat, git

from agent_comms.cli_commands import StopCliCommand, TargetBatchResult
from agent_comms.goal_attempts import GoalAttemptStore
from agent_comms.installer.backup import StoreBackup, bus_files
from agent_comms.installer.refusals import UpgradeRefused
from agent_comms.installer.runtime_resolution import RuntimeResolution
from agent_comms.installer.carry import STEPS, CarryChain, FinishedCarryStore
from agent_comms.installer.probes.carry import Carried, Carry, MigrateOperation
from agent_comms.installer.probes.commands import CliOutput, CliUndecodableError
from agent_comms.installer.commands import InstallCommands
from agent_comms.installer.interpreter_operation import InstallClock, TargetProgram
from agent_comms.installer.practice import PracticeCopy
from agent_comms.installer.upgrade import (
    ExistingBusInstall,
    PracticeBus,
    PreparedMigration,
    RehearsalFiles,
    Stack,
    StepRun,
    TargetTree,
    carried,
)
from agent_comms.launch_runtime import LaunchRuntime, RestoredRuntime
from agent_comms.maintenance_barrier import (
    DrainingPhase,
    MaintenanceBarrier,
    MaintenanceOperator,
    ReadyPhase,
)
from agent_comms.registry_store import RegistryStore
from agent_comms.root_runtime import RootRuntime, RootRuntimeStore
from agent_comms.thread_execution import ExternalThreadExecution
from agent_comms.thread_status import RunningThreadStatus
from agent_comms.threads import Thread
from conftest import ledger_one_failure_short
from fixture_git import IDENTITY

REPO = Path(__file__).resolve().parents[1]


def step_source() -> str:
    return (
        "from agent_comms.installer.probes.carry import Carried, Carry\n\n"
        "class Step(Carry):\n"
        "    opened_files = ()\n"
        "    def carry(self, *, apply):\n        return Carried(0)\n"
    )


class Repository:
    def __init__(self, path: Path):
        self.path = path
        self.git("init", "-q")

    def git(self, *args: str) -> str:
        return subprocess.check_output(["git", *IDENTITY, "-C", str(self.path), *args], text=True, timeout=10).strip()

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





def record_source(version: int, fields: str) -> str:
    return ("from dataclasses import dataclass\nfrom typing import Annotated, ClassVar\n"
            "from agent_comms.field_codec import Id128, Id128Text\n\n"
            "class RootKind(Id128):\n    \"\"\"Another kind written in the same spelling.\"\"\"\n\n"
            f"@dataclass(frozen=True)\nclass Record:\n    version: ClassVar[int] = {version}\n{fields}")


PLAIN = "    root: str\n"
SPELLED = "    root: Annotated[str, Id128Text]\n"


@pytest.mark.refactor_guard
@pytest.mark.parametrize("before, version, fields, step, uncarried", [
    (PLAIN, 1, PLAIN + "    label: str\n", {}, 1),
    (PLAIN, 2, PLAIN + "    label: str\n", {}, 1),
    (PLAIN, 2, PLAIN + "    label: str\n", {"carry_record": step_source()}, 0),
    (PLAIN, 1, PLAIN + "    kind: ClassVar[str] = 'x'\n", {}, 0),
    (SPELLED, 1, "    root: RootKind\n", {}, 0),
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
        {UncarriedRecordFormat.total(): 0}, {UncarriedRecordFormat.total(): uncarried})


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
    with InstallCommands() as commands:
        yield Stack(REPO, commands, home=tmp_path,
                    runtime=RestoredRuntime(LaunchRuntime.running().interpreter))


@pytest.fixture(scope="module")
def noisy_step(tmp_path_factory):
    tree = TargetTree(REPO, tmp_path_factory.mktemp("noisy-step"), "noisy-step")
    (tree.path / "tools" / "cutover").mkdir(parents=True)
    (tree.path / "tools" / "cutover" / "__init__.py").touch()
    (tree.path / "src").symlink_to(REPO / "src", target_is_directory=True)
    module = tree.path / "tools" / "cutover" / "carry_noisy.py"
    module.write_text(
        "from pathlib import Path\n"
        "from agent_comms.installer.probes.carry import Carry, Carried\n"
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


@pytest.fixture
def staged(stack, noisy_step):
    """The runtime built from the noisy tree takes its carry steps from that tree."""
    tree, _ = noisy_step
    return replace(stack, program=TargetProgram(tree.steps_package))


def test_step_answers_are_separate_from_diagnostics_and_keep_their_types(
        staged, tmp_path, noisy_step):
    """Every invocation crosses the real worker process, including a noisy migration."""
    _, step = noisy_step
    root = tmp_path / "bus"
    root.mkdir()
    request = step(root, root, tmp_path / "spill")
    assert staged.call_steps((request,), MigrateOperation(False)) == (
        Carried(1, {"applied": False}, (Path("saved"),)),)
    assert not (root / "saved").exists()
    assert staged.call_steps((request,), MigrateOperation(True)) == (
        Carried(1, {"applied": True}, (Path("saved"),)),)
    assert (root / "saved").read_text() == "migrated"


def test_a_bus_dry_runs_only_the_steps_it_has_not_finished(staged, tmp_path, noisy_step):
    """A step's own dry run still says work remains, so a skipped run proves the record
    answered; the record keeps a step that has since left the tree."""
    _, step = noisy_step
    root = tmp_path / "bus"
    root.mkdir()
    practice = PracticeBus(tmp_path, PracticeCopy(root, tmp_path / "copy"), root,
                           tmp_path / "spill", InstallClock())
    chain = CarryChain((step,))
    with InstallClock().selected():
        assert practice.pending(chain, staged) == (step,)
        chain.finish_at(root)
        assert practice.pending(chain, staged) == ()
    CarryChain(()).finish_at(root)
    assert FinishedCarryStore.at(root).read().steps == (step.declared_name,)


def test_a_backup_undoes_a_real_worker_failure(comms, staged, noisy_step):
    _, step = noisy_step
    saved = comms.root / "saved"
    saved.write_text("original")
    (comms.root / "fail").touch()
    maintenance = MaintenanceBarrier(RegistryStore.at(comms.root).path)
    hold = maintenance.begin(MaintenanceOperator.current())
    with hold.holding():
        backup = StoreBackup.take(
            comms.root, comms.root / "no-route", (saved,), comms.root.parent / "spill")
        migration = PreparedMigration.capture(hold, staged)
        with pytest.raises(UpgradeRefused, match="failed after changing the saved file"):
            migration.carry((step(comms.root, comms.root, comms.root.parent / "spill"),), staged)
        assert saved.read_text() == "migrated"
        hold = backup.restore(partial(staged.restore_stores, hold))
        assert saved.read_text() == "original"
        assert staged.verify(comms.root) == migration.read
    maintenance.clear(hold)


def test_restart_selection_uses_receiving_declaration_with_connected_cli_inbox(comms, stack):
    from agent_comms.child_process import ProcessIdentity

    process = ProcessIdentity.capture(os.getpid())
    native = comms.registry.declare(replace(Thread(
        'native-copy', frozenset(), str(comms.root)), process_identity=process))
    external = comms.registry.declare(Thread(
        'cli-inbox-copy', frozenset(), str(comms.root), execution=ExternalThreadExecution))
    with comms.registry.external_process(external.name, process):
        assert stack.running_owners(comms.root) == [native.name]
        native.execution.require_native()
        with pytest.raises(ValueError):
            external.execution.require_native()


def test_owner_stop_batch_reports_refusal_after_stopping_available_owners(comms, stack):
    names = ["failed", "other", "last"]
    for name in names[1:]:
        comms.registry.declare(
            Thread(name, frozenset(), str(comms.root)), RunningThreadStatus()
        )

    # A missing target must not prevent the batch from stopping its available owners.
    with pytest.raises(UpgradeRefused, match="failed"):
        for outcome in stack.owners(comms.root, StopCliCommand, names):
            outcome.require_success(UpgradeRefused)
    saved = comms.registry.snapshot()
    assert all(saved.status(name).stopped for name in names[1:])


def test_the_install_start_stops_owners_on_a_ledger_the_outgoing_runtime_cannot_open(
        comms, stack):
    """Install 14: f23ca8968 refused this ledger at every opening, so its stop answered
    nothing. The install's start holds the root, and the target stops the owners under
    that hold, carrying the ledger, which stays held until the install releases it."""
    from agent_comms.child_process import SpawnedProcess

    path = ledger_one_failure_short(comms.root)
    maintenance = MaintenanceBarrier(RegistryStore.at(comms.root).path)
    with SpawnedProcess.launch(("sleep", "600")) as owner:
        comms.registry.declare(replace(Thread("unit", frozenset(), str(comms.root)),
                                       process_identity=owner.identity),
                               RunningThreadStatus())
        with ExistingBusInstall.owners_stopped(comms.root, stack, stack) as owners:
            assert owners.stopped == ["unit"]
            assert stack.running_owners(comms.root) == []
            assert maintenance.read().phase is DrainingPhase
            owners.retired()
            owners.release()
            assert maintenance.read().phase is ReadyPhase
    GoalAttemptStore.at(path.parent)


def test_a_command_line_that_dies_before_answering_refuses_with_its_own_error():
    """Install 14: the outgoing runtime's stop crashed opening its ledger and printed nothing;
    the refusal names the crash instead of an empty document."""
    died = CliOutput(1, "", "StorageUncertainError: Unsupported goal attempt schema")
    with pytest.raises(CliUndecodableError, match="StorageUncertainError"):
        died.answer(TargetBatchResult)


def test_upgrade_refuses_at_start_without_its_own_uv(stack):
    """The installer names its own uv, even when no installed runtime exists."""
    assert not stack.uv.exists()
    with pytest.raises(UpgradeRefused):
        stack.require_uv()



def bus_installed_at(root: Path, commit: str) -> Path:
    """A bus whose runtime was built from ``commit``, as an install leaves it."""
    prefix = root.parent / f"runtime-{commit[:12]}"
    prefix.mkdir(parents=True)
    record = RuntimeResolution(core_commit=commit, packages={})
    (prefix / "build.json").write_text(record.model_dump_json())
    RootRuntimeStore.at(root).replace(RootRuntime(prefix))
    return root


def test_an_install_refuses_a_bus_that_never_held_a_retired_step(tmp_path):
    """Laptop, 10-24: its bus was built at f3194f95f while `git pull` had moved the checkout
    to 456726ace, past the 37 steps that bus never ran. The guard asks the bus, not HEAD."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    repository = Repository(checkout)
    before = repository.commit({"carry_first": step_source()})
    held = repository.commit({"carry_retired": step_source()})
    target = repository.commit({"carry_retired": None})
    deleting = target[:12]
    assert repository.git("rev-parse", "HEAD") == target

    CarryChain.require_crossed(checkout, bus_installed_at(tmp_path / "held/bus", held), target)
    stale = bus_installed_at(tmp_path / "before/bus", before)
    with pytest.raises(UpgradeRefused) as refused:
        CarryChain.require_crossed(checkout, stale, target)
    assert str(refused.value) == (
        "install: refused: "
        f"This bus was last installed at {before[:12]} and never ran 1 retired carry step(s): "
        f"tools/cutover/carry_retired.py (deleted by {deleting}; install {deleting}^ first)")
