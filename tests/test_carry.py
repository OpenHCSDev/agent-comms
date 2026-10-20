"""The carry family: one chain of idempotent steps, a per-bus ledger, and steps that never change."""

import importlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest

import agent_comms.private_nk_entrypoint as private_nk_entrypoint
from agent_comms.activity import Activity, ActivityLog, ActivityState
from agent_comms.runtime_updates import ThreadOutput
from agent_comms.turn_phase import ModelWaitPhase, ToolRunningPhase
from agent_comms.comms import Comms
from agent_comms.coordination_database import CoordinationStore
from agent_comms.external_transcript import ExternalTranscript
from agent_comms.goals import Goal
from agent_comms.maintenance_barrier import (
    MaintenanceBarrier,
    MaintenanceHeldError,
    MaintenanceOperator,
)
from agent_comms.read_basis import (
    ChannelConversation, ConversationCursor, DMConversation, ReadCursor,
)
from agent_comms.private_path import FileIdentity
from agent_comms.read_ledger import ReadDocument, ReadLedger
from agent_comms.recorded_bus import RecordedBus
from agent_comms.registry_store import RegistryNeedsConversion, RegistryStore
from agent_comms.thread_execution import ExternalThreadExecution
from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.todos import GoalRef, Todo, TodoStore
from cutover.carry import STEPS, Carry, CarryChain, CarryLedger, UnorderedSteps
from tools.debt_ratchet import EditedCarry, UncarriedRecordFormat, git
from agent_comms.recorded_time import RecordedEventTime
from test_todos import order, thread
from cutover.backup import Backup, UpgradeRefused, bus_files, transcripts_held
from upgrade_bus import (
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
    for step in CarryChain.at(REPO, "HEAD").steps:
        module = importlib.import_module(f"cutover.{step.name}")
        (declared,) = [value for value in vars(module).values() if isinstance(value, type)
                       and issubclass(value, Carry) and value.__module__ == module.__name__]
        assert (declared.__name__, declared.name()) == (step.declaration, step.name)
        assert declared.__mro__[1].__name__ == step.family.__name__


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
    with pytest.raises(ValueError, match="skips a step"):
        chain.pending(("carry_a", "carry_c"))


def test_steps_one_commit_adds_together_are_refused(tmp_path):
    repo = Repository(tmp_path)
    for name in ("carry_a", "carry_b"):
        (tmp_path / STEPS).mkdir(parents=True, exist_ok=True)
        (tmp_path / STEPS / f"{name}.py").write_text(step_source())
    repo.commit({})
    with pytest.raises(UnorderedSteps, match="in one commit"):
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
    with pytest.raises(ValueError, match="does not declare"):
        chain.pending(("carry_a", "carry_z"))
    with pytest.raises(ValueError, match="skips a step"):
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
    with pytest.raises(ValueError, match="skips a step"):
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
    with pytest.raises(ValueError, match="carry_script holds no Carry step"):
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
    assert carried(root, before, steps) == {"identical": 0, "rewritten": 3}
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
        assert carried(tmp_path, before, [{"rewritten": []}]) == {"identical": 1, "rewritten": 0}
        opened.execute("UPDATE todo SET id = 2")
        with pytest.raises(UpgradeRefused, match=r"\['todos.sqlite3'\]"):
            carried(tmp_path, before, [{"rewritten": []}])


def todos_before_order_runs(root: Path, scratch: Path) -> None:
    """A todo store as the live bus held it: its order declares no run (before P511), and it
    lacks the verdicts, corrections and messages columns; releases are already carried."""
    lead = thread("lead", scratch, 1)
    todo = TodoStore(scratch).create("one", "OpenHCSDev/agent-comms", "Repair the thread list",
        creator=lead, owner=lead, generation="one", deadline=RecordedEventTime(1000),
        order=order(scratch), goal=GoalRef("lead", 1.0, "goal-1"))
    with closing(sqlite3.connect(TodoStore.path_in(root))) as db:
        db.execute(Todo.schema_objects()[Todo.declared_name])
        todo.upsert(db)
        db.execute("""UPDATE todo SET "order" = json_remove("order", '$.run')""")
        for column in ("messages", "corrections", "verdicts"):
            db.execute(f"ALTER TABLE todo DROP COLUMN {column}")
        db.commit()


def run_step(name: str, root: Path, *flags: str) -> dict:
    environment = {**os.environ, "PYTHONPATH": os.pathsep.join([str(REPO / "src"), str(REPO / "tools")])}
    output = subprocess.check_output(
        [sys.executable, "-m", f"cutover.{name}", str(root), *flags], env=environment, cwd=REPO, timeout=60)
    return json.loads(output)


def retained_read_ledger(root: Path) -> None:
    """The earlier bus already ran the retired read-key step."""
    user = RecordedThreadIncarnation("user", 1.5)
    direct = DMConversation((RecordedThreadIncarnation("alice", 2.5), user))
    ReadLedger.at(root).replace(ReadDocument(
        messages={user: ReadCursor({ChannelConversation("#comms"): ConversationCursor(2, (4,)),
                            direct: ConversationCursor(0, (3,))})},
        bus_identity=RecordedBus(FileIdentity(66308, 6861023)),
        transcripts={("user", "/sessions/a.jsonl", 6860966): 4881511},
    ))


def test_every_step_carries_an_older_bus_once_and_writes_only_what_it_names(tmp_path, monkeypatch, capsys):
    """Storage roots/read keys are current; the remaining carries preserve retained data.
    Todo messages are installed before orders validate the current declaration."""
    tmp_path.chmod(0o700)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(private_nk_entrypoint, "pinned_native_package", lambda: tmp_path)
    root = tmp_path / "bus"
    comms = Comms(root)
    root_id = comms.messaging.initialize_private_initial_protocol()
    # carry_goal_wait_owners ran live in upgrade 5, when an absent goal was None; a released
    # step is never edited, so this thread declares a goal.
    worker = comms.registry.new_thread("worker", frozenset(), str(tmp_path))
    comms.registry.declare(replace(worker, goal=Goal("Repair the thread list", "goal-1", 1)))
    registry = json.loads((root / "registry.json").read_text())
    registry["threads"]["worker"]["earlier_projects"] = ["/earlier/project"]
    store = RegistryStore(root / "registry.json")
    with store.locked():
        store._write_unlocked(json.dumps(registry, indent=2))
    retained_read_ledger(root)
    todos_before_order_runs(root, tmp_path / "earlier")
    route = tmp_path / ".local/state/agent-comms/active-route.json"
    route.parent.mkdir(parents=True, mode=0o700)
    route.write_text(json.dumps({"version": 1, "root": str(root), "wire_root_id": root_id,
                                 "native_package": str(tmp_path)}))
    route.chmod(0o600)

    chain = CarryChain.at(REPO, "HEAD").steps
    steps = {step.name: step.current(REPO) for step in chain}

    def invoke(name, *flags):
        # Exercise the real CLI owner without starting a fresh interpreter for every
        # dry/apply/repeat. The target-src test separately proves process isolation.
        with monkeypatch.context() as call:
            call.setattr(sys, "argv", [name, str(root), *flags])
            steps[name].main()
        return json.loads(capsys.readouterr().out)

    for name in steps:
        before = bus_files(root)
        carried(root, before, [invoke(name)])
        applied = invoke(name, "--apply")
        carried(root, before, [applied])
        assert invoke(name)["changed"] == 0, name
    assert CarryLedger(root).applied() == tuple(step.name for step in chain)

    (thread,) = Comms(root).registry.snapshot().threads.values()
    assert thread.earlier_projects == (Path("/earlier/project"),)
    user = RecordedThreadIncarnation("user", 1.5)
    direct = DMConversation((RecordedThreadIncarnation("alice", 2.5), user))
    reads = ReadLedger.at(root).read()
    assert reads.messages == {user: ReadCursor({ChannelConversation("#comms"): ConversationCursor(2, (4,)),
                            direct: ConversationCursor(0, (3,))})}
    assert reads.transcripts == {("user", "/sessions/a.jsonl", 6860966): 4881511}
    assert json.loads(route.read_text()) == {"version": 2, "root": str(root), "wire_root_id": root_id}
    assert route.stat().st_mode & 0o777 == 0o600
    with closing(sqlite3.connect(TodoStore.path_in(root))) as db:
        (todo,) = Todo.stream(db)
    assert (todo.id, todo.corrections, todo.messages) == ("one", (), ())


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
    for step in chain[:-1]:
        CarryLedger(root).record(step.name)
    assert chain[-1].name == "carry_registry_version"

    before = bus_files(root)
    carried(root, before, [run_step("carry_registry_version", root)])
    applied = run_step("carry_registry_version", root, "--apply")
    assert carried(root, before, [applied]) == {"identical": len(before) - 3, "rewritten": 3}
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
    carried(root, before, [applied])
    assert applied["changed"] == carried_count
    assert run_step("carry_turn_records", root)["changed"] == 0
    (thread,) = Comms(root).registry.snapshot().threads.values()
    assert not thread.executing


def test_upgrade_stops_and_starts_exactly_the_captured_set_concurrently(comms, tmp_path, monkeypatch):
    from threading import Barrier

    import upgrade_bus
    from agent_comms.maintenance_barrier import ReadyPhase

    names = [f"worker-{number}" for number in range(19)]
    stop = Barrier(len(names), timeout=10)
    start = Barrier(len(names), timeout=10)
    calls = []
    commands = []
    carried_bus = []

    class Installed(Stack):
        def cli(self, root, *arguments):
            if arguments == ("threads",):
                assert not calls  # Never rediscover a possibly changed owner set.
                return {"threads": [
                    *({"name": name, "role": "agent", "status": "running"} for name in names),
                    {"name": "idle", "role": "agent", "status": "stopped"},
                    {"name": "human", "role": "human", "status": "running"},
                ]}
            command, flag, name = arguments
            assert flag == "--name" and name in names
            if command == "stop":
                stop.wait()  # A serial stop loop cannot reach this barrier.
            else:
                assert command == "start" and carried_bus
                start.wait()  # Nor can a serial start loop.
            calls.append((command, name))
            return {}

        def relock(self):
            assert commands[-1][-5:] == (
                "submodule", "update", "--init", "--recursive", "--checkout")

        def prepare_native(self):
            pass

        def verify(self, root):
            return {"threads": len(names)}

    def carry(root, pending, staged, revisions):
        assert sorted(calls) == sorted(("stop", name) for name in names)
        carried_bus.append(root)
        return []

    monkeypatch.setattr(upgrade_bus, "carry_all", carry)
    monkeypatch.setattr(upgrade_bus, "run", lambda *command, **options: commands.append(command))
    monkeypatch.setattr(upgrade_bus, "ROUTE", tmp_path / "absent-route.json")
    monkeypatch.setattr(upgrade_bus.HostService, "restart_running", staticmethod(lambda python: []))
    monkeypatch.setattr(upgrade_bus.PlannedInstallDeadline, "plan", lambda self, root: None)
    stack = Installed(tmp_path / "checkout")
    result = upgrade_bus.live(stack.checkout, comms.root, "old", "new", (), stack, stack, None, "worker")
    assert result["stopped_and_started"] == names
    assert sorted(calls) == sorted((command, name) for command in ("stop", "start") for name in names)
    assert MaintenanceBarrier(comms.root / "registry.json").read().phase is ReadyPhase
    assert commands[0] == ("git", "-C", stack.checkout, "merge", "--ff-only", "-q", "new")


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

    with pytest.raises(UpgradeRefused, match="stop failed"):
        Installed(REPO).owners(tmp_path, "stop", names)
    assert sorted(finished) == sorted(names)


def test_selected_revision_prepares_pins_and_cannot_return_a_failed_cached_tree(tmp_path, monkeypatch):
    import upgrade_bus

    commands = []

    def run(*command):
        commands.append(command)
        return ""

    monkeypatch.setattr(upgrade_bus, "run", run)
    revisions = Revisions(tmp_path / "repo", tmp_path / "trees")
    selected = revisions.tree("abcdef123456")
    assert commands == [
        ("git", "-C", revisions.repo, "worktree", "add", "--detach", "-q", selected, "abcdef123456"),
        ("git", "-C", selected, "submodule", "update", "--init", "--recursive", "--checkout"),
    ]

    def missing_dependency(*command):
        assert command[3:] == ("submodule", "update", "--init", "--recursive", "--checkout")
        raise UpgradeRefused("missing pinned dependency")

    monkeypatch.setattr(upgrade_bus, "run", missing_dependency)
    with pytest.raises(UpgradeRefused, match="missing pinned dependency"):
        revisions.tree("abcdef123456")
    assert revisions.trees == {"abcdef123456": selected}  # Failed preparation still owns cleanup.


def test_failed_carry_preserves_changed_bus_and_main_under_maintenance(comms, tmp_path, monkeypatch):
    import upgrade_bus
    from agent_comms.maintenance_barrier import ReadyPhase

    datum = comms.root / "kept.txt"
    datum.write_text("before")
    original_inode = comms.root.stat().st_ino
    commands = []

    class Installed(Stack):
        def cli(self, root, *arguments):
            assert arguments == ("threads",)
            return {"threads": []}

        def relock(self):
            pass

        def prepare_native(self):
            pass

    def failed_carry(*args):
        datum.write_text("changed before failure")
        raise RuntimeError("carry failed after writing")

    monkeypatch.setattr(upgrade_bus, "run", lambda *command, **options: commands.append(command))
    monkeypatch.setattr(upgrade_bus, "carry_all", failed_carry)
    monkeypatch.setattr(upgrade_bus, "ROUTE", tmp_path / "route.json")
    stack = Installed(tmp_path / "checkout")
    with pytest.raises(RuntimeError, match="carry failed after writing"):
        upgrade_bus.live(stack.checkout, comms.root, "old", "new", (), stack, stack, None, "worker")
    assert datum.read_text() == "changed before failure"
    assert comms.root.stat().st_ino == original_inode
    assert not any("reset" in command for command in commands)
    assert MaintenanceBarrier(comms.root / "registry.json").read().phase is not ReadyPhase
    assert list(comms.root.parent.glob(f"{comms.root.name}.upgrade-*/bus.tar"))


def test_upgrade_refuses_at_start_without_its_own_uv(tmp_path):
    """The 22:20 install ran a uv from another agent's scratch; the installer names its own."""
    stack = Stack(REPO, tmp_path)
    with pytest.raises(UpgradeRefused, match=re.escape(str(stack.uv))):
        stack.require_uv()


def test_upgrade_transcript_hold_covers_the_backup_and_its_restore(comms):
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
        backup = Backup.take(comms.root, comms.root.parent / "no-route.json",
                             (comms.root,), comms.root.parent / 'backup')
        assert bridge_publishes(comms.root).endswith("database is locked")
        assert carried(comms.root, before, []) == {"identical": len(before), "rewritten": 0}
        backup.restore()  # the carry aborted
        failed, = comms.root.parent.glob(f".{comms.root.name}.failed-*")
        assert bridge_publishes(failed).endswith("database is locked")
    assert bridge_publishes(comms.root) == "wrote"
    name = str(ExternalTranscript.path_in(comms.root).relative_to(comms.root))
    assert backup.files() == before
    assert CoordinationStore.digest(ExternalTranscript.path_in(failed)) == before[name]


def test_rehearsal_uses_and_cleans_the_requested_parent(tmp_path, monkeypatch):
    import upgrade_bus

    root = tmp_path / "original"
    root.mkdir()
    requested = tmp_path / "copies"
    requested.mkdir()
    destinations = []

    def refused_copy(source, destination):
        assert source == root
        assert destination.parent.parent == requested
        destinations.append(destination)
        raise RuntimeError("copy refused")

    monkeypatch.setattr(upgrade_bus, "copy_bus", refused_copy)
    with pytest.raises(RuntimeError, match="copy refused"):
        upgrade_bus.rehearse(root, (), Stack(tmp_path), None, requested)
    assert len(destinations) == 1
    assert list(requested.iterdir()) == []
    assert root.is_dir()


def rehearsed(stack: Stack, root: Path) -> dict:
    from upgrade_bus import rehearse
    return rehearse(root, (), stack, None, root.parent)


def installed_live(stack: Stack, root: Path) -> dict:
    from upgrade_bus import live
    return live(stack.checkout, root, "old", "new", (), stack, stack, None, "worker")


@pytest.mark.parametrize("install", [rehearsed, installed_live])
def test_a_step_writing_the_transcript_store_runs_after_the_hold(comms, tmp_path, monkeypatch, install):
    """The d88a12185 rehearsal: ToolCallIndex's BEGIN IMMEDIATE timed out against the
    installer's own transcript hold, which then stayed up across every carry."""
    import upgrade_bus
    from cutover.carry_tool_call_index import ToolCallIndex

    unit = comms.registry.declare(comms.registry.new_thread(
        "index-unit", frozenset({"index"}), str(comms.root), execution=ExternalThreadExecution))
    with ExternalTranscript(comms.root, unit.incarnation).writing():
        pass
    path = ExternalTranscript.path_in(comms.root)
    with closing(CoordinationStore.writer(path)) as db, db:
        for (index,) in db.execute("SELECT name FROM sqlite_master WHERE type = 'index' "
                                   "AND sql IS NOT NULL").fetchall():
            db.execute(f'DROP INDEX "{index}"')
    monkeypatch.setattr(CoordinationStore, "writer_timeout", 0.1)
    with transcripts_held(comms.root), pytest.raises(sqlite3.OperationalError, match="locked"):
        ToolCallIndex.carry(comms.root, apply=True)

    def tool_call_index(root, pending, staged, revisions):
        return [ToolCallIndex.carry(root, apply=True).report(ToolCallIndex.name(), True)]

    class Installed(Stack):
        def cli(self, root, *arguments):
            return {"threads": []}

        def relock(self):
            pass

        def prepare_native(self):
            pass

        def verify(self, root):
            return {"read": ExternalTranscript.path_in(root).exists()}

    monkeypatch.setattr(upgrade_bus, "carry_all", tool_call_index)
    monkeypatch.setattr(upgrade_bus, "run", lambda *command, **options: "")
    # A rehearsal places the route under its own home, so the route sits under this one.
    monkeypatch.setattr(upgrade_bus, "ROUTE", upgrade_bus.ROUTE.with_name(f"absent-{tmp_path.name}.json"))
    monkeypatch.setattr(upgrade_bus.HostService, "restart_running", staticmethod(lambda python: []))
    monkeypatch.setattr(upgrade_bus.PlannedInstallDeadline, "plan", lambda self, root: None)
    result = install(Installed(tmp_path / "checkout", tmp_path / "home"), comms.root)
    step, = result["steps"]
    assert step["changed"] == step["indexes"] > 0
    assert result["files"]["rewritten"] == 1
    assert result["read"] == {"read": True}


def test_a_writer_during_the_drain_is_refused_or_its_file_is_declared(comms, monkeypatch):
    unit = comms.registry.declare(comms.registry.new_thread(
        "drain-unit", frozenset({"drain"}), str(comms.root), execution=ExternalThreadExecution))
    transcript = ExternalTranscript(comms.root, unit.incarnation)
    output = ThreadOutput(transcript, comms.agents)
    output.enter(ToolRunningPhase("before the drain"))
    with transcript.writing():
        pass
    MaintenanceBarrier(comms.root / "registry.json").begin(MaintenanceOperator.current())
    before = bus_files(comms.root)
    monkeypatch.setattr(CoordinationStore, "writer_timeout", 0.1)
    with transcripts_held(comms.root):
        with pytest.raises(MaintenanceHeldError):
            output.enter(ModelWaitPhase("a hook on this checkout"))
        # A bridge started before the barrier existed writes activity without asking it.
        ActivityLog.at(comms.root).emit(Activity.create(unit.name, ActivityState.THINKING))
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            transcript.finish(turn_id="turn", item_id="during", events=())
        assert carried(comms.root, before, []) == {"identical": len(before), "rewritten": 0}
