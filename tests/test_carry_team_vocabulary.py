"""The lead's stored names become the team's: every store keeps its rows and files, and the
current decoders read them after the carry and reconcile."""

import json
import sqlite3
from contextlib import closing

from cutover.carry_team_vocabulary import (
    TeamVocabulary, TeamVocabularyFiles, TeamVocabularyMessages, TeamVocabularyTodos)

from agent_comms.bus import Bus
from agent_comms.channel_targets import ThreadTarget
from agent_comms.comms import wire
from agent_comms.locked_store import LiveFilesRequest
from agent_comms.messages import Message
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.service_passes import ServicePassStore
from agent_comms.task_attachments.team_pings import TeamUpdate, TeamUpdateNotice, TeamUpdateStore
from agent_comms.todos import OwnerRequest, TodoStore
from conftest import maintenance_held
from test_reconcile_stores import reconcile

PREVIOUS_TAGS = {new: old for old, new in TeamVocabulary.tags.items()}
PREVIOUS_KEYS = {new: old for old, new in TeamVocabulary.keys.items()}


def previous(value):
    """The form the previous runtime stored: the inverse of the carry's renaming."""
    match value:
        case dict():
            return {PREVIOUS_TAGS.get(key, PREVIOUS_KEYS.get(key, key)): previous(item)
                    for key, item in value.items()}
        case list():
            return [previous(item) for item in value]
        case str():
            return PREVIOUS_TAGS.get(value, value)
    return value


def carried(step, root):
    assert step(LiveFilesRequest(root), root / "spill").carry(apply=False).changed
    with maintenance_held(root) as holder:
        step(LiveFilesRequest(root), root / "spill").carry(apply=True)
        completed = reconcile(root, holder)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert step(LiveFilesRequest(root), root / "spill").carry(apply=False).changed == 0


def test_team_packets_in_history_and_their_projection(team_bus):
    root = team_bus.root
    teammate = team_bus.registry.require("unit-x").incarnation
    packet = TeamUpdateNotice(teammate, ())
    packet.publish(team_bus.messaging, ThreadTarget(teammate.name), packet.body(()))
    (sent,) = TeamUpdate.published(team_bus, teammate)
    path = Bus.path_in(root)
    table = Message.table
    with closing(Bus.writer(path)) as db, Bus.write_transaction(db):
        stored = db.execute(f'SELECT seq, task FROM "{table.declared_name}"'
                            " WHERE task LIKE '%team_notice%'").fetchall()
        TeamVocabulary.rewrite_stored(db, table, FieldCodec.field_plan(table, "task"), [
            (json.dumps(previous(json.loads(task))), seq) for seq, task in stored])
        db.execute('ALTER TABLE "team_update_position" RENAME TO "commander_update_position"')

    carried(TeamVocabularyMessages, root)

    assert TeamUpdate.published(wire(root), teammate) == (sent,)


def test_owner_requests_answer_for_their_teammate(team_bus):
    root = team_bus.root
    teammate = team_bus.registry.require("unit-x").acting_identity
    request = team_bus.goals.todos.add_request(teammate, "Keep the board honest")
    with closing(sqlite3.connect(TodoStore.path_in(root))) as db, db:
        db.execute('ALTER TABLE "owner_request" RENAME COLUMN "teammate" TO "commander"')
        db.execute('ALTER TABLE "owner_request" RENAME COLUMN "teammate_created_at" TO "commander_created_at"')

    carried(TeamVocabularyTodos, root)

    assert wire(root).goals.todos.open_requests(teammate) == (request,)
    with closing(TodoStore.read_snapshot(TodoStore.path_in(root))) as db:
        assert "teammate" in OwnerRequest.stored_columns(db)


def test_retained_packets_passes_and_channel_move_to_team_names(team_bus):
    root = team_bus.root
    teammate = team_bus.registry.require("unit-x").incarnation
    packet = TeamUpdateNotice(teammate, ())
    TeamUpdateStore.record(root, packet)
    store = TeamUpdateStore.for_team(root, teammate).path
    retired = root / "commander_updates" / store.name
    retired.parent.mkdir()
    retired.write_text(json.dumps(previous(json.loads(store.read_text()))))
    store.unlink()
    passes = ServicePassStore.path_in(root)
    passes.write_text(json.dumps({"failing": {"commander_heartbeat": {"since": 1.0, "error": "x"}},
                                  "commander_completed": []}))
    (root / "commander_frames").mkdir()
    (root / "commander_frames" / "old.json").write_text("{}")

    carried(TeamVocabularyFiles, root)

    assert TeamUpdateStore.for_team(root, teammate).read() == (packet,)
    assert not (root / "commander_updates").exists() and not (root / "commander_frames").exists()
    stored = FieldCodec.parse(passes.read_text())
    assert "team_heartbeat" in stored["failing"] and "teammate_completed" in stored
