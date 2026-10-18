"""The carry family: one chain of idempotent steps, a per-bus ledger, and steps that never change."""

import importlib
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

import agent_comms.private_nk_entrypoint as private_nk_entrypoint
from agent_comms.comms import Comms
from agent_comms.read_basis import Conversation, ReadCursor
from agent_comms.read_ledger import ReadLedger
from agent_comms.registry_store import RegistryStore
from agent_comms.thread_identity import RecordedThreadIncarnation
from cutover.carry import STEPS, Carry, CarryChain, CarryLedger
from tools.debt_ratchet import EditedCarry, UncarriedRecordFormat, git
from tools.upgrade_bus import Revisions, Stack, UpgradeRefused, before_stop, bus_files, carried, carry_all

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
    assert chain.steps[0].after is None
    assert [step.after for step in chain.steps[1:]] == [step.name for step in chain.steps[:-1]]
    for step in chain.steps:
        if step.revision == "HEAD":
            (declared,) = [value for value in vars(importlib.import_module(f"cutover.{step.name}")).values()
                           if isinstance(value, type) and issubclass(value, Carry) and value is not Carry]
            assert (declared.name(), declared.after) == (step.name, step.after)


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
    tail = repo.commit({"carry_c": None})
    assert [(step.name, step.revision) for step in CarryChain.at(tmp_path, "HEAD").steps] == [
        ("carry_a", held), ("carry_b", "HEAD"), ("carry_c", repo.git("rev-parse", f"{tail}^"))]


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
    assert CarryLedger(ledgerless).applied() is None
    chain = CarryChain.at(repo.path, "HEAD")
    for step in chain.pending(CarryLedger(ledgerless).applied() or ()):
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


def test_ledger_appends_each_step_once_and_owner_only(tmp_path):
    ledger = CarryLedger(tmp_path)
    assert ledger.applied() is None
    for name in ("carry_a", "carry_b", "carry_a"):
        ledger.record(name)
    assert ledger.applied() == ("carry_a", "carry_b")
    assert ledger.path.stat().st_mode & 0o777 == 0o600


@pytest.mark.refactor_guard
@pytest.mark.parametrize("change, edited", [
    ({"carry_b": step_source("carry_a")}, 0),
    ({"carry_a": step_source(None) + "# changed\n"}, 1),
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
    older = repo.commit({"carry": (REPO / STEPS / "carry.py").read_text()})
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


def run_step(name: str, root: Path, *flags: str) -> dict:
    environment = {**os.environ, "PYTHONPATH": os.pathsep.join([str(REPO / "src"), str(REPO / "tools")])}
    output = subprocess.check_output(
        [sys.executable, "-m", f"cutover.{name}", str(root), *flags], env=environment, cwd=REPO, timeout=60)
    return json.loads(output)


def earlier_read_ledger(root: Path) -> None:
    channel = {"target": "#comms", "participants": []}
    direct = {"target": "", "participants": [["alice", 2.5], ["user", 1.5]]}
    (root / "read_ledger.json").write_text(json.dumps({
        "messages": {
            json.dumps(["user", 1.5, channel], separators=(",", ":")): [1, 2, 4],
            json.dumps(["user", 1.5, direct], separators=(",", ":")): [3],
        },
        "bus_identity": [66308, 6861023],
        "transcripts": {json.dumps(["user", "/sessions/a.jsonl", 6860966]): 4881511},
        "notice": None,
    }))


def test_every_step_carries_an_older_bus_once_and_writes_only_what_it_names(tmp_path, monkeypatch):
    """Earlier formats: storage roots with a checkout list, a JSON-text-keyed read
    ledger and a route naming its native package. Each declared step dry-runs,
    applies, then finds nothing left; no file it does not name changes."""
    tmp_path.chmod(0o700)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(private_nk_entrypoint, "pinned_native_package", lambda: tmp_path)
    root = tmp_path / "bus"
    comms = Comms(root)
    root_id = comms.messaging.initialize_private_initial_protocol()
    comms.registry.declare(comms.registry.new_thread("worker", frozenset(), str(tmp_path)))
    registry = json.loads((root / "registry.json").read_text())
    registry["threads"]["worker"]["storage_roots"]["checkouts"] = [str(tmp_path), "/earlier/project"]
    store = RegistryStore(root / "registry.json")
    with store.locked():
        store._write_unlocked(json.dumps(registry, indent=2))
    earlier_read_ledger(root)
    route = tmp_path / ".local/state/agent-comms/active-route.json"
    route.parent.mkdir(parents=True, mode=0o700)
    route.write_text(json.dumps({"version": 1, "root": str(root), "wire_root_id": root_id,
                                 "native_package": str(tmp_path)}))
    route.chmod(0o600)

    steps = [step.name for step in CarryChain.at(REPO, "HEAD").steps if step.revision == "HEAD"]
    for name in steps:
        before = bus_files(root)
        carried(root, before, [run_step(name, root)])
        applied = run_step(name, root, "--apply")
        carried(root, before, [applied])
        assert run_step(name, root)["changed"] == 0, name
    assert CarryLedger(root).applied() == tuple(steps)

    (thread,) = Comms(root).registry.snapshot().threads.values()
    assert thread.earlier_projects == (Path("/earlier/project"),)
    user = RecordedThreadIncarnation("user", 1.5)
    direct = Conversation("", (RecordedThreadIncarnation("alice", 2.5), user))
    reads = ReadLedger.at(root).read()
    assert reads.messages == {user: {Conversation("#comms"): ReadCursor(2, (4,)), direct: ReadCursor(0, (3,))}}
    assert reads.transcripts == {("user", "/sessions/a.jsonl", 6860966): 4881511}
    assert json.loads(route.read_text()) == {"version": 2, "root": str(root), "wire_root_id": root_id}
    assert route.stat().st_mode & 0o777 == 0o600


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
