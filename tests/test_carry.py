"""The carry family: one chain of idempotent steps, a per-bus ledger, and steps that never change."""

import importlib
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.comms import Comms
from agent_comms.external_transcript import ExternalTranscript
from agent_comms.maintenance_barrier import (
    MaintenanceBarrier,
)
from agent_comms.registry_store import RegistryNeedsConversion, RegistryStore
from agent_comms.thread_execution import ExternalThreadExecution
from cutover.carry import STEPS, Carry, CarryChain, CarryLedger, UnorderedSteps
from tools.debt_ratchet import EditedCarry, UncarriedRecordFormat, git
from cutover.backup import (
    StoreBackup, UpgradeRefused, bus_files, transcripts_held,
)
from upgrade_bus import (
    RehearsalFiles,
    Revisions,
    Stack,
    before_stop,
    carried,
    carry_all,
)

REPO = Path(__file__).resolve().parents[1]


def step_source(family: str = "Carry") -> str:
    return (
        f"from cutover.carry import Carried, {family}\n\n"
        f"class Step({family}):\n"
        "    @classmethod\n    def carry(cls, root, *, apply):\n        return Carried(0)\n"
    )


class Repository:
    def __init__(self, path: Path):
        self.path = path
        self.git("init", "-q")
        self.git("config", "user.email", "carry@example.invalid")
        self.git("config", "user.name", "Carry test")
        declarations = self.path / STEPS / "carry.py"
        declarations.parent.mkdir(parents=True, exist_ok=True)
        declarations.write_bytes((REPO / STEPS / "carry.py").read_bytes())

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


def test_each_step_at_head_is_the_one_carry_its_module_declares():
    from cutover import carry as carry_module

    for step in CarryChain.at(REPO, "HEAD").steps:
        module = importlib.import_module(f"cutover.{step.name}")
        (declared,) = [value for value in vars(module).values() if isinstance(value, type)
                       and issubclass(value, Carry) and value.__module__ == module.__name__]
        assert (declared.__name__, declared.name()) == (step.declaration, step.name)
        # Step.family is compiled from the historical header, not the imported class.
        assert issubclass(declared, getattr(carry_module, step.family.__name__))


def test_target_parent_and_its_ordering_arrive_with_first_step(tmp_path):
    repo = Repository(tmp_path)
    repo.commit({"carry_rows": step_source("TodoRows")})
    declarations = tmp_path / STEPS / "carry.py"
    declarations.write_text(
        declarations.read_text()
        + "\nraise AssertionError('target module must never be imported')\n"
        + "class FutureParent(Carry):\n"
        + "    @classmethod\n"
        + "    def precedes(cls, later):\n"
        + "        return issubclass(later, TodoRows)\n"
    )
    head = repo.commit({"carry_new": step_source("FutureParent")})
    chain = CarryChain.at(tmp_path, head)
    assert [step.name for step in chain.steps] == ["carry_new", "carry_rows"]
    assert chain.steps[0].family.__name__ == "FutureParent"


def test_steps_run_in_the_order_the_main_line_merged_them(tmp_path):
    """A merge brings in its side's steps in the order that side added them."""
    repo = Repository(tmp_path)
    repo.commit({"carry_a": step_source()})
    repo.git("checkout", "-qb", "side")
    repo.commit({"carry_c": step_source(), "carry_d": step_source()})
    repo.git("checkout", "-q", "-")
    repo.commit({"carry_b": step_source()})
    repo.git("merge", "-q", "--no-ff", "--no-edit", "side")
    chain = CarryChain.at(tmp_path, "HEAD")
    assert [step.name for step in chain.steps] == ["carry_a", "carry_b", "carry_c", "carry_d"]
    with pytest.raises(ValueError):
        chain.pending(("carry_a", "carry_c"))


def test_steps_one_commit_adds_together_are_refused(tmp_path):
    repo = Repository(tmp_path)
    for name in ("carry_a", "carry_b"):
        (tmp_path / STEPS).mkdir(parents=True, exist_ok=True)
        (tmp_path / STEPS / f"{name}.py").write_text(step_source())
    repo.commit({})
    with pytest.raises(UnorderedSteps):
        CarryChain.at(tmp_path, "HEAD")


def test_a_deleted_step_is_retired_and_never_runs_again(tmp_path):
    repo = Repository(tmp_path)
    repo.commit({"carry_a": step_source(), "carry_b": step_source(), "carry_c": step_source()})
    repo.commit({"carry_b": None, "carry_d": step_source()})
    chain = CarryChain.at(tmp_path, "HEAD")
    assert [step.name for step in chain.steps] == ["carry_a", "carry_c", "carry_d"]
    assert chain.retired == {"carry_b"}
    assert [step.name for step in chain.pending(())] == ["carry_a", "carry_c", "carry_d"]
    assert [step.name for step in chain.pending(("carry_a", "carry_b", "carry_c"))] == ["carry_d"]
    assert [step.name for step in chain.pending(("carry_a",))] == ["carry_c", "carry_d"]
    with pytest.raises(ValueError):
        chain.pending(("carry_a", "carry_z"))
    with pytest.raises(ValueError):
        chain.pending(("carry_c",))


def test_a_todo_column_runs_before_the_row_steps_merged_ahead_of_it(tmp_path):
    """Row steps read todos as today's Todo, so a later column step moves ahead of them;
    a bus that ran those row steps before the column step existed still takes it."""
    repo = Repository(tmp_path)
    repo.commit({"carry_a": step_source(), "carry_rows": step_source("TodoRows"),
                 "carry_b": step_source(), "carry_column": step_source("TodoColumnCarry")})
    chain = CarryChain.at(tmp_path, "HEAD")
    assert [step.name for step in chain.steps] == ["carry_a", "carry_column", "carry_rows", "carry_b"]
    assert [step.name for step in chain.pending(("carry_a", "carry_rows", "carry_b"))] == [
        "carry_column"]
    with pytest.raises(ValueError):
        chain.pending(("carry_rows",))


def test_a_step_applies_only_after_every_earlier_step_and_a_ledgerless_bus_upgrades_in_order(tmp_path):
    (tmp_path / "repo").mkdir()
    repo = Repository(tmp_path / "repo")
    runnable = "\nif __name__ == '__main__':\n    Step.main()\n"
    repo.commit({name: step_source() + runnable for name in ("carry_a", "carry_b", "carry_c")})
    environment = {**os.environ, "PYTHONPATH": os.pathsep.join(
        [str(REPO / "src"), str(REPO / "tools"), str(repo.path / "tools")])}

    def apply(name: str, root: Path) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(repo.path / STEPS / f"{name}.py"), str(root), "--apply"],
                              env=environment, cwd=repo.path, capture_output=True, text=True, timeout=60)

    skipped = tmp_path / "skipped"
    skipped.mkdir()
    CarryLedger(skipped).record("carry_b")
    for name in ("carry_a", "carry_c"):
        refused = apply(name, skipped)
        assert refused.returncode and "Bus ledger skips a step" in refused.stderr
    assert CarryLedger(skipped).applied() == ("carry_b",)

    ledgerless = tmp_path / "ledgerless"
    ledgerless.mkdir()
    assert "must first run ['carry_a', 'carry_b']" in apply("carry_b", ledgerless).stderr
    assert CarryLedger(ledgerless).applied() == ()
    chain = CarryChain.at(repo.path, "HEAD")
    for step in chain.pending(CarryLedger(ledgerless).applied()):
        assert apply(step.name, ledgerless).returncode == 0
    assert CarryLedger(ledgerless).applied() == ("carry_a", "carry_b", "carry_c")


def test_a_carry_module_outside_the_chain_is_refused(tmp_path):
    """A conversion written as a standalone script would never run on an upgraded bus."""
    repo = Repository(tmp_path)
    repo.commit({"carry_a": step_source(),
                 "carry_script": "def carry(root):\n    pass\n"})
    with pytest.raises(ValueError):
        CarryChain.at(tmp_path, "HEAD")


def test_ledger_appends_each_step_once_and_owner_only(tmp_path):
    ledger = CarryLedger(tmp_path)
    assert ledger.applied() == ()
    for name in ("carry_a", "carry_b", "carry_a"):
        ledger.record(name)
    assert ledger.applied() == ("carry_a", "carry_b")
    assert ledger.path.stat().st_mode & 0o777 == 0o600


@pytest.mark.refactor_guard
@pytest.mark.parametrize("change, edited", [
    ({"carry_b": step_source()}, 0),
    ({"carry_a": step_source().replace("Carried(0)", "Carried(1)")}, 1),
    ({"carry_a": step_source().replace("(Carry):\n", "(Carry):\n    after = 'carry_previous'\n")}, 0),
    ({"carry_a": step_source() +
      "\n    @classmethod\n    def backup_files(cls, root):\n        return (root / 'registry.json',)\n"}, 0),
    ({"carry_a": step_source().replace("Carried(0)", "Carried(1)") +
      "\n    @classmethod\n    def backup_files(cls, root):\n        return (root / 'registry.json',)\n"}, 1),
    ({"carry_a": step_source() +
      "\n    @classmethod\n    def before_stop(cls, root, *, apply):\n        return Carried(1)\n"}, 1),
    ({"carry_a": None, "carry_renamed": step_source()}, 1),
    ({"carry_a": None}, 0),
])
def test_a_step_on_main_is_never_edited_renamed_or_reused(tmp_path, change, edited):
    repo = Repository(tmp_path)
    base = repo.commit({"carry_a": step_source()})
    head = repo.commit(change)
    assert EditedCarry.compare(tmp_path, base, head, set(), "src") == (
        {"EditedCarry": 0}, {"EditedCarry": edited})


@pytest.mark.refactor_guard
def test_a_deleted_step_name_is_never_reused(tmp_path):
    repo = Repository(tmp_path)
    repo.commit({"carry_a": step_source()})
    deleted = repo.commit({"carry_a": None})
    readded = repo.commit({"carry_a": step_source()})
    assert EditedCarry.compare(tmp_path, deleted, readded, set(), "src")[1] == {"EditedCarry": 1}


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


class CheckoutStack(Stack):
    """A checkout's stack running this test's interpreter in place of its synced one."""

    @property
    def python(self) -> Path:
        return Path(sys.executable)


def test_an_older_checkout_upgrades_through_both_passes_with_the_target_src(tmp_path):
    """The installed checkout's src lacks what the new step imports; both passes import
    the src of the tree that declares the step."""
    (tmp_path / "checkout/src").mkdir(parents=True)
    repo = Repository(tmp_path / "checkout")
    (repo.path / "src/probe_api.py").write_text("OLD = 1\n")
    older = repo.commit({name: (REPO / STEPS / f"{name}.py").read_text()
                         for name in ("carry", "backup")})
    (repo.path / "src/probe_api.py").write_text("OLD = 1\nNEW = 2\n")
    newer = repo.commit({"carry_probe": (
        "from probe_api import NEW\n\nfrom cutover.carry import Carried, Carry\n\n\n"
        "class Probe(Carry):\n"
        "    @classmethod\n    def before_stop(cls, root, *, apply):\n"
        "        (root / 'stopped').write_text(str(NEW))\n        return Carried(1, {}, ('stopped',))\n\n"
        "    @classmethod\n    def carry(cls, root, *, apply):\n"
        "        (root / 'carried').write_text(str(NEW))\n        return Carried(1, {}, ('carried',))\n\n\n"
        "if __name__ == '__main__':\n    Probe.main()\n")})
    repo.git("checkout", "-q", older)
    root = tmp_path / "bus"
    root.mkdir()
    pending = CarryChain.at(repo.path, newer).pending(())
    revisions = Revisions(repo.path, tmp_path / "trees")
    try:
        installed = CheckoutStack(repo.path, home=tmp_path)
        before = bus_files(root)
        steps = [*before_stop(root, pending, installed, revisions), *carry_all(root, pending, installed, revisions)]
    finally:
        revisions.close()
    assert carried(root, before, steps, RehearsalFiles.declared(steps)) == RehearsalFiles(
        0, 3, ("carried", "carries.json", "stopped"), 0)
    with pytest.raises(UpgradeRefused):
        carried(root, before, steps, frozenset({CarryLedger.name}))
    assert (root / "stopped").read_text() == (root / "carried").read_text() == "2"
    assert CarryLedger(root).applied() == ("carry_probe",)


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
        steps = [{"rewritten": []}]
        assert carried(tmp_path, before, steps, RehearsalFiles.declared(steps)) == (
            RehearsalFiles(1, 0, (), 0))
        opened.execute("UPDATE todo SET id = 2")
        with pytest.raises(UpgradeRefused):
            carried(tmp_path, before, steps, RehearsalFiles.declared(steps))


def run_step(name: str, root: Path, *flags: str) -> dict:
    environment = {**os.environ, "PYTHONPATH": os.pathsep.join([str(REPO / "src"), str(REPO / "tools")])}
    output = subprocess.check_output(
        [sys.executable, "-m", f"cutover.{name}", str(root), *flags], env=environment, cwd=REPO, timeout=60)
    return json.loads(output)


def test_an_unversioned_registry_is_converted_by_its_step_and_then_reads(tmp_path, monkeypatch):
    """The installer's carries convert a P669-old registry, so its later reads succeed."""
    tmp_path.chmod(0o700)
    monkeypatch.setenv("HOME", str(tmp_path))
    root = tmp_path / "bus"
    comms = Comms(root)
    comms.messaging.initialize_private_initial_protocol()
    comms.registry.declare(comms.registry.new_thread("worker", frozenset(), str(tmp_path)))
    store = RegistryStore.at(root)
    old = json.loads(store.path.read_text())
    old.pop("version")
    old["threads"]["worker"]["goal"] = {}
    with store.locked():
        store._write_unlocked(json.dumps(old, indent=2))
    with pytest.raises(RegistryNeedsConversion):
        RegistryStore.at(root).read()
    chain = CarryChain.at(REPO, "HEAD").steps
    position = next(i for i, step in enumerate(chain)
                    if step.name == "carry_registry_version")
    for step in chain[:position]:
        CarryLedger(root).record(step.name)

    before = bus_files(root)
    dry = [run_step("carry_registry_version", root)]
    carried(root, before, dry, RehearsalFiles.declared(dry))
    applied = run_step("carry_registry_version", root, "--apply")
    changed = [applied]
    result = carried(root, before, changed, RehearsalFiles.declared(changed))
    assert result.identical == len(before) - 3 and result.rewritten == 3
    assert result.restore_bytes > 0
    assert run_step("carry_registry_version", root, "--apply")["changed"] == 0
    (thread,) = Comms(root).registry.snapshot().threads.values()
    assert thread.goal.to_wire() is None


@pytest.mark.parametrize("stored, carried_count", [(None, 1), ({"turn_id": "t"}, None)])
def test_turn_records_drop_an_idle_stored_turn_and_refuse_a_held_one(tmp_path, monkeypatch, stored, carried_count):
    """Before P482 a thread stored active_turn; an idle one carries to NoTurnRecord once."""
    tmp_path.chmod(0o700)
    monkeypatch.setenv("HOME", str(tmp_path))
    root = tmp_path / "bus"
    comms = Comms(root)
    comms.messaging.initialize_private_initial_protocol()
    comms.registry.declare(comms.registry.new_thread("worker", frozenset(), str(tmp_path)))
    registry = json.loads((root / "registry.json").read_text())
    registry["threads"]["worker"].pop("turn_record", None)
    registry["threads"]["worker"]["active_turn"] = stored
    store = RegistryStore(root / "registry.json")
    with store.locked():
        store._write_unlocked(json.dumps(registry, indent=2))
    chain = [step.name for step in CarryChain.at(REPO, "HEAD").steps]
    for earlier in chain[: chain.index("carry_turn_records")]:
        CarryLedger(root).record(earlier)
    if carried_count is None:
        with pytest.raises(subprocess.CalledProcessError):
            run_step("carry_turn_records", root, "--apply")
        assert json.loads((root / "registry.json").read_text()) == registry
        return
    before = bus_files(root)
    applied = run_step("carry_turn_records", root, "--apply")
    changed = [applied]
    carried(root, before, changed, RehearsalFiles.declared(changed))
    assert applied["changed"] == carried_count
    assert run_step("carry_turn_records", root)["changed"] == 0
    (thread,) = Comms(root).registry.snapshot().threads.values()
    assert not thread.executing


def test_restart_selection_uses_receiving_declaration_with_connected_cli_inbox(comms):
    from agent_comms.child_process import ProcessIdentity

    process = ProcessIdentity.capture(os.getpid())
    native = comms.registry.declare(replace(comms.registry.new_thread(
        'native-copy', frozenset(), str(comms.root)), process_identity=process))
    external = comms.registry.declare(comms.registry.new_thread(
        'cli-inbox-copy', frozenset(), str(comms.root), execution=ExternalThreadExecution))
    with comms.registry.external_process(external.name, process):
        assert Stack(REPO).running_owners(comms.root) == [native.name]
        native.execution.require_native()
        with pytest.raises(ValueError):
            external.execution.require_native()


def test_owner_request_failure_joins_the_entire_set_before_propagating(tmp_path):
    from threading import Barrier

    names = ["failed", "other", "last"]
    admitted = Barrier(len(names), timeout=10)
    finished = []

    class Installed(Stack):
        def cli(self, root, *arguments):
            admitted.wait()
            name = arguments[-1]
            finished.append(name)
            if name == "failed":
                raise UpgradeRefused("stop failed")
            return {}

    with pytest.raises(UpgradeRefused):
        Installed(REPO).owners(tmp_path, "stop", names)
    assert sorted(finished) == sorted(names)


def test_upgrade_refuses_at_start_without_its_own_uv(tmp_path):
    """The 22:20 install ran a uv from another agent's scratch; the installer names its own."""
    stack = Stack(REPO, tmp_path)
    with pytest.raises(UpgradeRefused):
        stack.require_uv()


def test_upgrade_transcript_hold_covers_selected_snapshot(comms):
    """A bridge publishes from its own process, and the upgrade reads the store's files raw
    for the backup and its digests. The bridge may not write while those are read, nor into
    the failed bus a restore keeps as evidence."""
    unit = comms.registry.declare(comms.registry.new_thread(
        "hold-unit", frozenset({"hold"}), str(comms.root), execution=ExternalThreadExecution))
    transcript = ExternalTranscript(comms.root, unit.incarnation)
    with transcript.writing():
        pass

    def bridge_publishes(bus: Path) -> str:
        writer = ("import sqlite3, sys\n"
                  "db = sqlite3.connect(sys.argv[1], timeout=0.2, isolation_level=None)\n"
                  "db.execute('BEGIN IMMEDIATE'); db.execute('COMMIT'); print('wrote')")
        result = subprocess.run([sys.executable, "-c", writer, ExternalTranscript.path_in(bus)],
                                capture_output=True, text=True, timeout=60)
        return result.stdout.strip() or result.stderr.strip().splitlines()[-1]

    with transcripts_held(comms.root):
        before = bus_files(comms.root)
        path = ExternalTranscript.path_in(comms.root)
        backup = StoreBackup.take(comms.root, comms.root.parent / "no-route.json",
                                  (path,), comms.root.parent / 'backup')
        assert bridge_publishes(comms.root).endswith("database is locked")
        assert carried(comms.root, before, [], RehearsalFiles.declared([])) == (
            RehearsalFiles(len(before), 0, (), 0))
        assert backup.original()
    assert bridge_publishes(comms.root) == "wrote"
    assert tuple(backup.files()) == (str(path.relative_to(comms.root)),)


def test_practice_preserves_old_maintenance_format_until_before_stop(tmp_path):
    from agent_comms.comms import Comms
    from agent_comms.field_codec import FieldCodec
    from agent_comms.maintenance_barrier import ReadyPhase
    from cutover.carry_maintenance_identity import MaintenanceIdentity, OldMarker, OldReadyState
    from install_gate import PracticeCopy

    source, destination = tmp_path / "source", tmp_path / "copy"
    source.mkdir()
    destination.mkdir()
    old = MaintenanceBarrier(source / "registry.json")
    marker = OldMarker(version=1, root=str(source), generation=7)
    state = OldReadyState(version=1, root=str(source), generation=7,
                         operator="old-installer", nonce="a" * 32, phase=ReadyPhase)
    for path, record in ((old.marker_path, marker), (old.state_path, state)):
        path.write_text(json.dumps(FieldCodec.encode(record)))
        path.chmod(0o600)
    original = tuple(path.read_bytes() for path in old.witness_paths)
    CarryLedger(source).record("carry_external_pages")
    PracticeCopy(Comms(source), destination).copy_install_state(destination)
    assert CarryLedger(destination).applied() == CarryLedger(source).applied()
    copied = MaintenanceBarrier(destination / "registry.json")
    for path in copied.witness_paths:
        assert json.loads(path.read_bytes())["version"] == 1
        assert path.stat().st_mode & 0o777 == 0o600
    assert MaintenanceIdentity.before_stop(destination, apply=True).changed == 1
    assert copied.read().version == 2
    assert original == tuple(path.read_bytes() for path in old.witness_paths)


