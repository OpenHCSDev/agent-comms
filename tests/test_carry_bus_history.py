"""The migration consumes a bus made by the predecessor's real publisher."""
from __future__ import annotations


import json
import os
import shutil
import subprocess
import sys
import tarfile
from dataclasses import replace
from pathlib import Path

import pytest

# This fixture needs the removed writer, not a copy of its implementation.
# The commit is an ancestor of this migration and is retired with the carry test.
PREDECESSOR = "196582f6d"
OUTGOING_V2 = "d9ce9cd95954a9846bc703129d55a4327f55674d"
REPOSITORY = Path(__file__).resolve().parents[1]


def publish_predecessor_history(directory: Path) -> None:
    from agent_comms.audience_manifest import FrozenRecipient
    from agent_comms.child_process import ProcessIdentity
    from agent_comms.comms import wire
    from agent_comms.coordination_tables.publications import PublicationIntents
    from agent_comms.coordination_tables.responses import ResponseObligation
    from agent_comms.goal_actions import SetGoalAction
    from agent_comms.goals import RequestGoalOrder
    from agent_comms.historical_views import HistoryArchive
    from agent_comms.installer.backup import StoreBackup
    from agent_comms.messages import InfoKind, Message
    from agent_comms.obligation_states import PublishingResponse
    from agent_comms.read_basis import AllConversation, ConversationCursor, ReadCursor
    from agent_comms.read_ledger import PendingReadNotice, ReadDocument, ReadLedger
    from agent_comms.reason_codes import NoReasonCode
    from agent_comms.recorded_bus import RecordedBusIdentity
    from agent_comms.response_acknowledgement import canonical_publication_key
    from agent_comms.response_conversation import ResponseConversation
    from conftest import publish_initial, responder
    from test_envelope_bus_integration import acquire_message

    directory.mkdir()
    project = directory / "project"
    project.mkdir()
    resource = project / "a.py"
    resource.write_text("original source\n")
    comms = wire(directory / "wire")
    for name in ("sender", "owner"):
        comms.registry.declare(comms.registry.new_thread(
            name, frozenset({"team"}), worktree=str(project),
            process_identity=ProcessIdentity.capture(os.getpid()),
        ))
    root_id = comms.messaging.initialize_private_initial_protocol(history=(
        Message("sender", "owner", "Retained public history: café", InfoKind, seq=1),
    ))
    question = publish_initial(comms, "sender", "owner", "A real private question")
    acquire_message(comms, "owner", resource)
    original = comms.bus.log.read_delivery_cohort(root_id, question.seq)
    recipient, = original.audience.recipients
    response = Message("owner", "sender", "A real keyed answer", InfoKind,
                       answers=(question.reference,))
    intent = PublicationIntents(
        "migration-proof", response.target, response,
        canonical_publication_key("migration-proof", response.target), response.message_id,
    )
    conversation = ResponseConversation(
        FrozenRecipient(recipient.recipient_lookup, "owner"),
        ResponseObligation("migration-proof", response.target, PublishingResponse(),
                           NoReasonCode(), 0, 0, 1),
        (original,),
    )
    bus = responder(comms.root, comms.registry)
    with bus.publisher.opened_activity() as activity, bus.log.locked():
        bus.publisher._publish_keyed_response_unlocked(
            intent, conversation=conversation, registry_snapshot=comms.registry.snapshot(),
            activity=activity,
        )
    comms.goals.update_goal("owner", SetGoalAction(
        text="Preserve this independent owner request", goal_id="retained-request",
        order=RequestGoalOrder(),
    ))
    owner = comms.registry.snapshot().require("owner").incarnation.require_recorded()
    document = ReadDocument(
        messages={owner: ReadCursor({AllConversation(): ConversationCursor(1, (3,))})},
        bus_identity=RecordedBusIdentity.capture(comms.bus.log.path),
        transcripts={("owner", "retained-transcript", 42): 17},
        notice=PendingReadNotice("Keep this unread notice"),
    )
    ReadLedger.at(comms.root).replace(document)
    destination = directory / "destination"
    StoreBackup.copy_tree(comms.root, destination)
    ReadLedger.at(destination).replace(replace(
        document, bus_identity=RecordedBusIdentity.capture(destination / "bus.jsonl")))
    archived = HistoryArchive(destination / "bus.jsonl").attach(comms.root)
    ReadLedger.at(archived.root).replace(replace(
        document, bus_identity=RecordedBusIdentity.capture(Path(archived.root) / "bus.jsonl")))
    empty = wire(directory / "empty")
    empty.messaging.initialize_private_initial_protocol(history=())


def extract_revision(repository: Path, revision: str, destination: Path) -> None:
    archive = destination.with_name(destination.name + ".tar")
    with archive.open("wb") as stream:
        subprocess.run(["git", "-C", repository, "archive", revision], stdout=stream, check=True)
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as tree:
        tree.extractall(destination, filter="data")
    archive.unlink()


def outgoing_source(destination: Path, revision: str) -> Path:
    """One outgoing tree owns its source, revision-pinned dependencies and wheel stamp."""
    from agent_comms.core_stamp import WheelStamp
    from agent_comms.repo import Checkout

    extract_revision(REPOSITORY, revision, destination)
    for module in Checkout(destination).submodules():
        relative = module.path.relative_to(destination)
        pin = subprocess.check_output(
            ["git", "-C", REPOSITORY, "ls-tree", revision, relative], text=True).split()[2]
        extract_revision(REPOSITORY / relative, pin, module.path)
    commit = subprocess.check_output(
        ["git", "-C", REPOSITORY, "rev-parse", revision], text=True).strip()
    WheelStamp(commit).write(destination / "src" / "agent_comms" / "package_stamp.py")
    return destination


def predecessor_history(tmp_path: Path, revision: str = PREDECESSOR) -> Path:
    """Create the original bus through its real publisher, shared by carry/rehearsal proof."""
    predecessor = outgoing_source(tmp_path / "publisher", revision)
    generated = tmp_path / "generated"
    runner = predecessor / Path(__file__).name
    shutil.copyfile(__file__, runner)
    result = subprocess.run(
        [sys.executable, str(runner), str(generated)],
        cwd=predecessor,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(
            (str(predecessor / "src"), str(predecessor / "tests")))},
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return generated


def test_predecessor_publisher_history_round_trips_into_bus(tmp_path):
    from cutover.carry_bus_history import (
        BusHistory,
        ClaimHistoryRow,
        HistoryLine,
        HistoryMetadata,
        HistoryOffset,
    )
    from cutover.carry_finished_turns import FinishedTurns
    from cutover.carry_owner_requests import OwnerRequests

    from agent_comms.bus import Bus, ClaimRecord, PublicRecord
    from agent_comms.errors import RelationViolationError, UnclaimedError
    from agent_comms.historical_views import HistoryArchive
    from agent_comms.installer.backup import StoreBackup, bus_files
    from agent_comms.installer.carry import Carried, CarryChain, MigrateOperation
    from agent_comms.installer.commands import InstallCommands
    from agent_comms.installer.interpreter_operation import TargetProgram
    from agent_comms.installer.upgrade import RehearsalFiles, Stack, StepRun, carried
    from agent_comms.launch_runtime import LaunchRuntime, RestoredRuntime
    from agent_comms.maintenance_barrier import MaintenanceBarrier, MaintenanceOperator
    from agent_comms.messages import BusRecord
    from agent_comms.read_ledger import ReadLedger
    from agent_comms.recorded_bus import RecordedBusIdentity
    from agent_comms.registry_store import RegistryStore
    from agent_comms.todos import TodoStore

    chain = CarryChain.at(REPOSITORY)
    assert (chain.steps.index(FinishedTurns) < chain.steps.index(OwnerRequests)
            < chain.steps.index(BusHistory))

    generated = predecessor_history(tmp_path)
    original = generated / "destination"
    archived, = (original / "history").iterdir()
    root = tmp_path / "carried"
    spill = tmp_path / "hdd-spill"
    spill.mkdir()
    StoreBackup.copy_tree(original, root)
    # This publisher authored a pre-carry registry: every step the chain orders before the
    # goal-request carry runs first, as the installer runs them.
    preceding = {}
    maintenance = MaintenanceBarrier(RegistryStore.at(root).path)
    hold = maintenance.begin(MaintenanceOperator.current())
    with hold.holding():
        for step in chain.steps[:chain.steps.index(OwnerRequests)]:
            carry = step(root, original, spill)
            dry_run = carry.carry(apply=False)
            preceding[step] = carry.carry(apply=True)
            assert preceding[step].detail == dry_run.detail
            assert carry.carry(apply=False).changed == 0
    maintenance.clear(hold)
    assert preceding[FinishedTurns].changed > 0
    relatives = (Path(), archived.relative_to(original))
    originals = {relative: (original / relative / BusHistory.filename).read_bytes()
                 for relative in relatives}
    before = {relative: ReadLedger.at(root / relative).read() for relative in relatives}
    identities = {relative: RecordedBusIdentity.capture(root / relative / BusHistory.filename)
                  for relative in relatives}
    modes = {relative: (root / relative / BusHistory.filename).stat().st_mode
             for relative in relatives}
    request = BusHistory(root, original, spill)
    pending = request.carry(apply=False)
    assert pending.changed > 0
    assert pending.detail == dict(rows=8, continuous=8, round_tripped=8)
    assert all(not Bus.path_in(root / relative).exists() for relative in relatives)
    assert not tuple(spill.iterdir())
    operation = MigrateOperation(True)
    requests = OwnerRequests(root, original, spill)
    request_dry_run = requests.carry(apply=False)
    assert request_dry_run.detail["requests"] == 2
    maintenance = MaintenanceBarrier(RegistryStore.at(root).path)
    hold = maintenance.begin(MaintenanceOperator.current())
    with hold.holding():
        saved = bus_files(root)
        request_result = requests.carry(apply=True)
        assert request_result.detail == request_dry_run.detail
        assert requests.carry(apply=False).changed == 0
    # The pass crosses the installer's own worker, as the staged runtime runs it.
    with hold.holding(), InstallCommands() as commands:
        staged = Stack(REPOSITORY, commands, home=tmp_path,
                       runtime=RestoredRuntime(LaunchRuntime.running().interpreter),
                       program=TargetProgram(REPOSITORY / "tools"))
        (result,) = staged.call_steps((request,), operation)
    steps = (StepRun(requests.declared_name, request_result), StepRun(request.declared_name, result))
    carried(root, saved, steps, RehearsalFiles.declared(steps))
    maintenance.clear(hold)
    assert result.changed == 8
    assert {key: result.detail[key] for key in ("rows", "continuous", "round_tripped")} == {
        "rows": 8, "continuous": 8, "round_tripped": 8,
    }
    assert result.detail["read_viewers_before"] == result.detail["read_viewers_after"] == 2
    assert (result.detail["read_conversations_before"]
            == result.detail["read_conversations_after"] == 2)
    assert result.detail["read_transcripts_before"] == result.detail["read_transcripts_after"] == 2
    assert not tuple(spill.iterdir())
    for relative in relatives:
        directory = root / relative
        registry = RegistryStore.at(directory).read()
        owner_requests = TodoStore(directory).open_requests(registry.threads["owner"].incarnation)
        assert len(owner_requests) == 1
        assert owner_requests[0].id.value == "retained-request"
        assert owner_requests[0].text == "Preserve this independent owner request"
        source = directory / BusHistory.filename
        metadata = HistoryMetadata.read(source.parent)
        assert RecordedBusIdentity.capture(source) == identities[relative]
        assert source.stat().st_mode == modes[relative]
        assert source.read_bytes() == originals[relative]
        assert (original / relative / BusHistory.filename).read_bytes() == originals[relative]
        bus = Bus(Bus.path_in(directory))
        rows = tuple(HistoryLine.rows(source, metadata))
        claim, = (line for line in rows if isinstance(line.row, ClaimHistoryRow))
        if claim.row.seq > metadata.claimed_after_seq:
            assert bus.claim(claim.row.seq) == claim.row.claim_transition.current()
            assert {type(bus.lookup(line.row.seq)) for line in rows} == set(
                BusRecord.members_with(BusRecord))
            with pytest.raises(
                RelationViolationError, match="record differs from its declared template",
            ):
                claim.verify(PublicRecord.from_message(claim.row))
        else:
            with pytest.raises(UnclaimedError):
                bus.claim(claim.row.seq)
            assert type(bus.lookup(claim.row.seq)) is PublicRecord
            assert not isinstance(bus.lookup(claim.row.seq), ClaimRecord)
        for line in rows:
            line.verify(bus.lookup(line.row.seq))
        with bus.observing() as db:
            assert HistoryOffset.count(db) == 4
            assert all(HistoryOffset.select(db, offset=line.offset) == [
                HistoryOffset(line.offset, line.row.seq)] for line in rows)
        assert BusHistory.transfer(source, bus, metadata).changed == 0
        assert ReadLedger.at(directory).read() == replace(
            before[relative], bus_identity=RecordedBusIdentity.capture(bus.path))
        assert BusHistory.rebind_reads(directory, before[relative].bus_identity).changed == 0
    converted, = HistoryArchive(Bus.path_in(root)).sources()
    converted.validate()
    assert converted.root == str(root / archived.relative_to(original))
    catalog = HistoryArchive.path_in(root)
    catalog_before = catalog.stat()
    assert request.carry(apply=True).changed == 0
    assert request.carry(apply=False).changed == 0
    assert catalog.stat().st_ino == catalog_before.st_ino
    assert catalog.stat().st_mtime_ns == catalog_before.st_mtime_ns
    assert request.after_acceptance().changed == 4
    staged, = spill.iterdir()
    for relative in relatives:
        assert not (root / relative / BusHistory.filename).exists()
        assert not (root / relative / HistoryMetadata.filename).exists()
        retained = staged / relative / BusHistory.filename
        assert retained.read_bytes() == originals[relative]
        assert retained.stat().st_mode & 0o777 == 0o400
    assert request.after_acceptance().changed == 0
    assert request.carry(apply=True) == request.carry(apply=False) == Carried(0)
    for relative in relatives:
        with Bus(Bus.path_in(root / relative)).observing() as db:
            assert not HistoryOffset.present(db)
    empty = generated / "empty"
    empty_request = BusHistory(empty, empty, spill)
    assert empty_request.carry(apply=False).changed > 0
    empty_request.carry(apply=True)
    assert Bus.path_in(empty).exists()
    assert empty_request.carry(apply=False).changed == 0
    assert empty_request.after_acceptance().changed == 2
    assert empty_request.carry(apply=False) == Carried(0)
    fresh = tmp_path / "fresh-sqlite"
    fresh.mkdir()
    fresh_bus = Bus.install(Bus.path_in(fresh))
    assert BusHistory(fresh, fresh, spill).carry(apply=True) == Carried(0)
    with fresh_bus.writing() as db:
        HistoryOffset.create(db)
    with pytest.raises(RelationViolationError, match="already staged"):
        BusHistory(fresh, fresh, spill).carry(apply=True)


def test_absent_history_leaves_schema_repair_to_reconcile(tmp_path):
    from cutover.carry_bus_history import BusHistory, HistoryOffset

    from agent_comms.bus import Bus, BusSchema, PublicRecord
    from agent_comms.messages import (Addressed)
    from agent_comms.errors import RelationViolationError
    from agent_comms.installer.backup import bus_files
    from agent_comms.installer.carry import Carried
    from agent_comms.messages import InfoKind, Message
    from agent_comms.typed_table import Index
    from conftest import maintenance_held
    from test_reconcile_stores import reconcile

    root = tmp_path / "wire"
    bus = Bus.install(Bus.path_in(root))
    original = bus.append(lambda seq, db: PublicRecord.from_message(
        Message("author", "worker", "Keep this original", InfoKind, seq=seq),
    ))
    index = Index(("target",)).name(Addressed.table)
    with bus.writing() as db:
        db.execute(f'DROP INDEX "{index}"')
    request = BusHistory(root, root, tmp_path / "spill")

    with maintenance_held(root) as nonce:
        before = bus_files(root)
        assert request.carry(apply=False) == request.carry(apply=True) == Carried(0)
        assert bus_files(root) == before
        completed = reconcile(root, nonce)
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert json.loads(completed.stdout)["rewritten"] == [bus.path.name]
        with bus.observing() as db:
            BusSchema.require_schema(db)
    assert bus.lookup(original.seq) == original

    with bus.writing() as db:
        HistoryOffset.create(db)
        db.execute(f'DROP INDEX "{index}"')
    for apply in (False, True):
        with pytest.raises(RelationViolationError, match="already staged"):
            request.carry(apply=apply)


if __name__ == "__main__":
    publish_predecessor_history(Path(sys.argv[1]))
