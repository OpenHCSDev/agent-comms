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
from agent_comms.cli_bridges.shared import ThreadOutput
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
from agent_comms.registry_store import RegistryStore
from agent_comms.rule_reviews import AnsweredRuleReviewEntry, DrawnRuleReviewEntry, RuleReviews
from agent_comms.thread_execution import ExternalThreadExecution
from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.todos import GoalRef, Todo, TodoStore
from cutover.carry import STEPS, Carry, CarryChain, CarryLedger
from tools.debt_ratchet import EditedCarry, UncarriedRecordFormat, git
from agent_comms.recorded_time import RecordedEventTime
from test_todos import order, thread
from cutover.backup import Backup, UpgradeRefused, bus_files, transcripts_held
from tools.upgrade_bus import (
    Revisions,
    Stack,
    before_stop,
    carried,
    carry_all,
)

REPO = Path(__file__).resolve().parents[1]


def step_source(after: str | None) -> str:
    return (
        "from cutover.carry import Carried, Carry\n\n"
        f"class Step(Carry):\n    after = {after!r}\n\n"
        "    @classmethod\n    def carry(cls, root, *, apply):\n        return Carried(0)\n"
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
        for name, source in steps.items():
            path = self.path / STEPS / f"{name}.py"
            if source is None:
                path.unlink()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(source)
        self.git("add", "--all")
        self.git("commit", "-qm", "steps")
        return self.git("rev-parse", "HEAD")


def test_steps_at_head_form_one_chain_and_each_module_declares_its_step():
    chain = CarryChain.at(REPO, "HEAD")
    names = [step.name for step in chain.steps]
    assert chain.steps[0].after is None
    assert [step.after for step in chain.steps[1:]] == names[:-1]
    for step in chain.steps:
        if step.revision == "HEAD":
            (declared,) = [value for value in vars(importlib.import_module(f"cutover.{step.name}")).values()
                           if isinstance(value, type) and issubclass(value, Carry) and value is not Carry]
            assert declared.name() == step.name
            assert declared.after == step.after


def test_retained_read_ledger_still_reaches_its_cursor_conversions():
    chain = CarryChain.at(REPO, "HEAD")
    pending = chain.pending(("carry_read_ledger",))
    names = [step.name for step in pending]
    assert names.index("carry_p495_read_ledger") < names.index("carry_archived_read_ledgers")
    assert names.index("carry_archived_read_ledgers") < names.index("carry_goal_wait_targets")
    assert all(step.revision != "HEAD" for step in pending
               if step.name in ("carry_p495_read_ledger", "carry_archived_read_ledgers"))


def test_a_deleted_step_runs_from_the_last_revision_that_held_it(tmp_path):
    repo = Repository(tmp_path)
    held = repo.commit({"carry_a": step_source(None), "carry_b": step_source("carry_a")})
    repo.commit({"carry_a": None, "carry_c": step_source("carry_b")})
    chain = CarryChain.at(tmp_path, "HEAD")
    assert [(step.name, step.revision) for step in chain.steps] == [
        ("carry_a", held), ("carry_b", "HEAD"), ("carry_c", "HEAD")]
    assert [step.name for step in chain.pending(())] == ["carry_a", "carry_b", "carry_c"]
    assert [step.name for step in chain.pending(("carry_a",))] == ["carry_b", "carry_c"]
    with pytest.raises(ValueError, match="skips a step"):
        chain.pending(("carry_b",))
    with pytest.raises(ValueError, match="does not declare"):
        chain.pending(("carry_a", "carry_z"))
    repo.commit({"carry_c": None})
    assert [(step.name, step.revision) for step in CarryChain.at(tmp_path, "HEAD").steps] == [
        ("carry_a", held), ("carry_b", "HEAD")]



def test_reconnected_successor_retires_deleted_steps_without_replaying_them(tmp_path):
    """Finished migrations leave Git history but no longer enter the executable chain."""
    repo = Repository(tmp_path)
    repo.commit({"carry_external_pages": step_source(None)})
    repo.commit({"carry_route_index": step_source("carry_external_pages"),
                 "carry_wake_index": step_source("carry_route_index")})
    repo.commit({"carry_route_index": None, "carry_wake_index": None})
    repo.commit({"carry_display_counts": step_source("carry_external_pages")})
    chain = CarryChain.at(tmp_path, "HEAD")
    assert [step.name for step in chain.steps] == ["carry_external_pages", "carry_display_counts"]
    assert [step.name for step in chain.pending(("carry_external_pages",))] == ["carry_display_counts"]
    with pytest.raises(ValueError, match="skips a step"):
        chain.pending(("carry_display_counts",))


def test_a_bus_that_ran_a_retired_step_plans_the_kept_steps_after_it(tmp_path):
    """The live shape: steps retired after a bus ran them, and a newer step placed
    before one it ran."""
    repo = Repository(tmp_path)
    repo.commit({"carry_pages": step_source(None)})
    repo.commit({"carry_index": step_source("carry_pages"),
                 "carry_run": step_source("carry_index")})
    repo.commit({"carry_messages": step_source("carry_run")})
    repo.commit({"carry_index": None, "carry_messages": step_source("carry_pages"),
                 "carry_run": step_source("carry_messages")})
    chain = CarryChain.at(tmp_path, "HEAD")
    assert [step.name for step in chain.steps] == ["carry_pages", "carry_messages", "carry_run"]
    assert chain.retired == {"carry_index"}
    assert [step.name for step in chain.pending(("carry_pages", "carry_index", "carry_run"))] == [
        "carry_messages"]
    with pytest.raises(ValueError, match="does not declare"):
        chain.pending(("carry_pages", "carry_index", "carry_never"))
    with pytest.raises(ValueError, match="skips a step"):
        chain.pending(("carry_run",))


def test_a_step_applies_only_after_every_earlier_step_and_a_ledgerless_bus_upgrades_in_order(tmp_path):
    (tmp_path / "repo").mkdir()
    repo = Repository(tmp_path / "repo")
    runnable = "\nif __name__ == '__main__':\n    Step.main()\n"
    repo.commit({"carry_a": step_source(None) + runnable, "carry_b": step_source("carry_a") + runnable,
                 "carry_c": step_source("carry_b") + runnable})
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


@pytest.mark.parametrize("steps", [
    {"carry_a": step_source(None), "carry_b": step_source(None)},
    {"carry_a": step_source(None), "carry_b": step_source("carry_a"), "carry_c": step_source("carry_a")},
])
def test_steps_that_do_not_form_one_chain_are_refused(tmp_path, steps):
    repo = Repository(tmp_path)
    repo.commit(steps)
    with pytest.raises(ValueError, match="one chain"):
        CarryChain.at(tmp_path, "HEAD")


def test_a_carry_module_outside_the_chain_is_refused(tmp_path):
    """A conversion written as a standalone script would never run on an upgraded bus."""
    repo = Repository(tmp_path)
    repo.commit({"carry_a": step_source(None),
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
    ({"carry_b": step_source("carry_a")}, 0),
    ({"carry_a": step_source(None).replace("Carried(0)", "Carried(1)")}, 1),
    ({"carry_a": step_source("carry_previous")}, 0),
    ({"carry_a": None, "carry_renamed": step_source(None)}, 1),
    ({"carry_a": None}, 0),
])
def test_a_step_on_main_is_never_edited_renamed_or_reused(tmp_path, change, edited):
    repo = Repository(tmp_path)
    base = repo.commit({"carry_a": step_source(None)})
    head = repo.commit(change)
    assert EditedCarry.compare(tmp_path, base, head, set(), "src") == (
        {"EditedCarry": 0}, {"EditedCarry": edited})


@pytest.mark.refactor_guard
def test_a_deleted_step_name_is_never_reused(tmp_path):
    repo = Repository(tmp_path)
    repo.commit({"carry_a": step_source(None)})
    deleted = repo.commit({"carry_a": None})
    readded = repo.commit({"carry_a": step_source(None)})
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
    (PLAIN, 2, PLAIN + "    label: str\n", {"carry_record": step_source(None)}, 0),
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
        "class Probe(Carry):\n    after = None\n\n"
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
    steps = {step.name: step.current(REPO) for step in chain if step.revision == "HEAD"}

    def invoke(name, *flags):
        # Exercise the real CLI owner without starting a fresh interpreter for every
        # dry/apply/repeat. The target-src test separately proves process isolation.
        with monkeypatch.context() as call:
            call.setattr(sys, "argv", [name, str(root), *flags])
            steps[name].main()
        return json.loads(capsys.readouterr().out)

    for name in steps:
        # The older bus ran each deleted step while the tree still held it.
        for deleted in chain[len(CarryLedger(root).applied()):]:
            if deleted.revision == "HEAD":
                break
            CarryLedger(root).record(deleted.name)
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


LIVE_DRIVER = """
import signal, sys
from pathlib import Path
from upgrade_bus import Stack, live, stopped


class Owners(Stack):
    def cli(self, root, *arguments):
        print(' '.join(arguments), flush=True)
        return {"threads": [{"name": "worker", "role": "agent", "status": "running"}]}


for each in (signal.SIGTERM, signal.SIGINT):
    signal.signal(each, stopped)
root = Path(sys.argv[1])
live(root, root, "", "", (), Owners(root), Owners(root), None, "worker")
"""


def test_upgrade_killed_while_waiting_clears_maintenance(tmp_path):
    """A reader holds the route; SIGTERM while the upgrade waits for it runs the abort: the bus
    reads READY and the stopped thread is started again."""
    import fcntl
    import signal

    from agent_comms.maintenance_barrier import MaintenanceBarrier, ReadyPhase

    root = tmp_path / "bus"
    Comms(root)
    routes = tmp_path / "home/.local/state/agent-comms"
    routes.mkdir(parents=True)
    reader = os.open(routes, os.O_RDONLY | os.O_DIRECTORY)
    fcntl.flock(reader, fcntl.LOCK_SH)
    try:
        tool = subprocess.Popen(
            [sys.executable, "-c", LIVE_DRIVER, str(root)], text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={**os.environ, "HOME": str(tmp_path / "home"),
                 "PYTHONPATH": os.pathsep.join(str(REPO / part) for part in ("src", "tools"))})
        waited = ""
        while "Waiting for default-route readers" not in waited:
            line = tool.stderr.readline()
            assert line, waited + tool.stdout.read()
            waited += line
        assert MaintenanceBarrier(root / "registry.json").read().phase is not ReadyPhase
        tool.send_signal(signal.SIGTERM)
        out, err = tool.communicate(timeout=60)
    finally:
        os.close(reader)
    assert tool.returncode != 0
    assert "stopped by SIGTERM" in err and "aborted before the backup" in err
    assert out.splitlines() == ["threads", "stop --name worker", "start --name worker"]
    assert MaintenanceBarrier(root / "registry.json").read().phase is ReadyPhase


@pytest.mark.parametrize("served_by,registered", [
    ("installed", True), ("other", True), ("installed", False),
])
def test_upgrade_round_trip_through_the_connector_ends_it_or_restores(
    comms, tmp_path, monkeypatch, served_by, registered,
):
    """The upgrade's last step reads the bus through the running connector service over its
    loopback endpoint. A service on another runtime than the installed one fails the upgrade
    through its restore, and the read leaves no message behind."""
    import site
    import socket
    import tools.upgrade_bus as upgrade_bus
    from agent_comms.channel_targets import CommandTarget
    from agent_comms.channels import ANY_CHANNEL
    from agent_comms.maintenance_barrier import MaintenanceBarrier, ReadyPhase

    comms.registry.declare(comms.registry.new_thread("user", frozenset(), str(comms.root)))
    comms.registry.declare(comms.registry.new_thread("claude-ai", frozenset(), str(comms.root)))
    if registered:
        reviews = RuleReviews(comms.root)
        reviews.write("claude-ai", DrawnRuleReviewEntry(("Declare each fact once",), 1))
        reviews.write("claude-ai", AnsweredRuleReviewEntry(
            1, "This check exercises upgrade_bus.py:158 through the connector's declared caller with its required rule review."))
    runtimes = {name: tmp_path / name / "bin/python" for name in ("installed", "other")}
    for runtime in runtimes.values():
        runtime.parent.mkdir(parents=True)
        runtime.symlink_to(sys.executable)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    class Owners(Stack):
        python = runtimes["installed"]

        def environment(self, *paths):
            environment = super().environment(*paths)
            return {**environment, "PYTHONPATH": os.pathsep.join(
                (environment["PYTHONPATH"], *site.getsitepackages()))}

        def cli(self, root, *arguments):
            return {"threads": []}

        def relock(self):
            pass

        def prepare_native(self):
            pass

        def sync(self):
            pass

        def verify(self, root):
            return {}

    ran = upgrade_bus.run
    monkeypatch.setattr(upgrade_bus, "run", lambda *command, **options: (
        "" if command[0] == "git" else ran(*command, **options)))
    monkeypatch.setattr(upgrade_bus, "route_readers", lambda: None)
    monkeypatch.setattr(upgrade_bus, "ROUTE", tmp_path / "route.json")
    stack = Owners(REPO, tmp_path / "home", port)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    service = subprocess.Popen(
        [runtimes[served_by], "-c", upgrade_bus.CONNECTOR, "serve", "--root", comms.root, "--port", str(port)],
        env={**stack.environment(), "PYTHONUNBUFFERED": "1"}, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True)
    try:
        started = ""
        while "Running on" not in started:
            line = service.stdout.readline()
            assert line, started
            started += line
        if not registered:
            # The service had its required caller at startup. The upgrade must
            # still refuse if that caller is no longer registered at call time.
            comms.registry.unregister("claude-ai")
            comms.registry.archive("claude-ai")
            comms.registry.begin_delete("claude-ai")
            comms.registry.remove("claude-ai")
        failed = comms.root.parent.glob(f".{comms.root.name}.failed-*")
        if not registered:
            with pytest.raises(UpgradeRefused, match="connector round trip was refused"):
                upgrade_bus.live(checkout, comms.root, "", "", (), stack, stack, None, "worker")
            assert len(list(failed)) == 1
        elif served_by == "installed":
            report = upgrade_bus.live(checkout, comms.root, "", "", (), stack, stack, None, "worker")
            assert json.loads(report["round_trip"]) == {
                "round_trip": f"comms_channels read alike through {runtimes['installed']}"}
            assert not list(failed)
        else:
            with pytest.raises(UpgradeRefused, match=f"runs {runtimes['other']}, not the installed"):
                upgrade_bus.live(checkout, comms.root, "", "", (), stack, stack, None, "worker")
            assert len(list(failed)) == 1
    finally:
        service.terminate()
        service.wait(timeout=30)
    reader = Comms(comms.root)
    assert [name for name in reader.registry.snapshot().threads
            if list(reader.bus.inbox(name, CommandTarget.named(ANY_CHANNEL.name)))] == []
    assert MaintenanceBarrier(comms.root / "registry.json").read().phase is ReadyPhase


def test_upgrade_refuses_at_start_without_its_own_uv(tmp_path):
    """The 22:20 install ran a uv from another agent's scratch; the installer names its own."""
    stack = Stack(REPO, tmp_path)
    with pytest.raises(UpgradeRefused, match=re.escape(str(stack.uv))):
        stack.require_uv()


def test_upgrade_transcript_hold_outlasts_the_backup_and_an_aborted_carry(comms):
    """A bridge publishes from its own process, and the upgrade reads the store's files raw
    for the backup and its digests. Neither may let the bridge write before the carry is
    judged, nor into the failed bus an abort keeps as evidence."""
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


def test_a_writer_during_the_drain_is_refused_or_its_file_is_declared(comms, monkeypatch):
    unit = comms.registry.declare(comms.registry.new_thread(
        "drain-unit", frozenset({"drain"}), str(comms.root), execution=ExternalThreadExecution))
    transcript = ExternalTranscript(comms.root, unit.incarnation)
    output = ThreadOutput(transcript, comms.agents)
    output.status(ActivityState.WORKING, "before the drain")
    with transcript.writing():
        pass
    MaintenanceBarrier(comms.root / "registry.json").begin(MaintenanceOperator("drain-test"))
    before = bus_files(comms.root)
    monkeypatch.setattr(CoordinationStore, "writer_timeout", 0.1)
    with transcripts_held(comms.root):
        with pytest.raises(MaintenanceHeldError):
            output.status(ActivityState.THINKING, "a hook on this checkout")
        # A bridge started before the barrier existed writes activity without asking it.
        ActivityLog.at(comms.root).emit(Activity.create(unit.name, ActivityState.THINKING))
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            transcript.finish(turn_id="turn", item_id="during", events=())
        assert carried(comms.root, before, []) == {"identical": len(before), "rewritten": 0}
