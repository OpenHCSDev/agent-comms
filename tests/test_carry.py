"""The carry family: one chain of idempotent steps, a per-bus ledger, and steps that never change."""

import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.external_transcript import ExternalTranscript
from agent_comms.thread_execution import ExternalThreadExecution
from agent_comms.installer.carry import (
    STEPS, Carried, CarryChain, CarryLedger,
)
from agent_comms.installer.backup import (
    StoreBackup, UpgradeRefused, bus_files, transcripts_held,
)
from agent_comms.installer.upgrade import (
    RehearsalFiles,
    Stack,
    StepRun,
    carried,
)
from tools.debt_ratchet import UncarriedRecordFormat, git

REPO = Path(__file__).resolve().parents[1]


def step_source() -> str:
    return (
        "from agent_comms.installer.carry import Carried, Carry\n\n"
        "class Step(Carry):\n"
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


def test_the_chain_is_every_declared_step_the_ledger_lacks():
    """Each carry module's one step is in the chain; a recorded name with no step is a
    finished step whose file was deleted, so it is ignored."""
    chain = CarryChain.at(REPO)
    assert sorted(step.name() for step in chain.steps) == sorted(
        path.stem for path in (REPO / STEPS).glob("carry_*.py"))
    assert chain.pending(("carry_long_deleted",)) == chain.steps
    assert chain.pending(tuple(step.name() for step in chain.steps)) == ()


def test_ledger_appends_each_step_once_and_owner_only(tmp_path):
    ledger = CarryLedger(tmp_path)
    assert ledger.applied() == ()
    for name in ("carry_a", "carry_b", "carry_a"):
        ledger.record(name)
    assert ledger.applied() == ("carry_a", "carry_b")
    assert ledger.path.stat().st_mode & 0o777 == 0o600


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
                                  (path,), comms.root.parent / 'spill')
        assert isinstance(backup, StoreBackup)
        assert bridge_publishes(comms.root).endswith("database is locked")
        assert carried(comms.root, before, [], RehearsalFiles.declared([])) == (
            RehearsalFiles(len(before), 0, (), 0))
        assert backup.original()
    assert bridge_publishes(comms.root) == "wrote"
    assert tuple(backup.files()) == (str(path.relative_to(comms.root)),)


