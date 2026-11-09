"""The lead's stored names become the team's: every store keeps its rows and files, and the
current decoders read them after the carry and reconcile."""

import json
import sqlite3
from contextlib import closing

from cutover.carry_team_vocabulary import (
    TeamVocabulary,
    TeamVocabularyFiles,
    TeamVocabularyMessages,
    TeamVocabularyTodos,
)

from agent_comms.comms import wire
from agent_comms.coordination.service_passes import ServicePassStore
from agent_comms.messaging.bus import Bus
from agent_comms.messaging.channel_targets import ThreadTarget
from agent_comms.messaging.messages import Message
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.planning.orders import (
    OwnerRequest,
    TodoStore,
)
from agent_comms.storage.locked_store import LiveFilesRequest
from agent_comms.storage.store_files import store_lock_path
from agent_comms.task_attachments.team_pings import TeamUpdate, TeamUpdateNotice, TeamUpdateStore
from agent_comms.team.team_hud import CodexUsage, TeamFrame, TeamHud, TeamPlan, UnreadableUsage
from agent_comms.team.vibe_check import OverloadDegradation
from conftest import maintenance_held
from test_reconcile_stores import reconcile

PREVIOUS_TAGS = {new: old for old, new in TeamVocabulary.tags.items()}
PREVIOUS_KEYS = {new: old for old, new in TeamVocabulary.keys.items()}


def previous(value):
    """The form the previous runtime stored: the inverse of the carry's renaming."""
    match value:
        case dict():
            return {
                PREVIOUS_TAGS.get(key, PREVIOUS_KEYS.get(key, key)): previous(item)
                for key, item in value.items()
            }
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
        stored = db.execute(
            f'SELECT seq, task FROM "{table.declared_name}"' " WHERE task LIKE '%team_notice%'"
        ).fetchall()
        TeamVocabulary.rewrite_stored(
            db,
            table,
            FieldCodec.field_plan(table, "task"),
            [(json.dumps(previous(json.loads(task))), seq) for seq, task in stored],
        )
        db.execute('ALTER TABLE "team_update_position" RENAME TO "commander_update_position"')

    carried(TeamVocabularyMessages, root)

    assert TeamUpdate.published(wire(root), teammate) == (sent,)


def test_owner_requests_answer_for_their_teammate(team_bus):
    root = team_bus.root
    teammate = team_bus.registry.require("unit-x").acting_identity
    request = team_bus.goals.todos.add_request(
        teammate, '{"commander": "Keep the historical text and the board honest"}')
    with closing(sqlite3.connect(TodoStore.path_in(root))) as db, db:
        db.execute('ALTER TABLE "owner_request" RENAME COLUMN "teammate" TO "commander"')
        db.execute(
            'ALTER TABLE "owner_request" RENAME COLUMN "teammate_created_at"'
            ' TO "commander_created_at"'
        )

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
    services = ServicePassStore.at(root)
    services.failed("TeamHeartbeat", KeyError("original branch read failed"))
    services.heartbeat_passed((teammate,), (teammate,))
    passes.write_text(json.dumps(previous(json.loads(passes.read_text()))))
    hud = TeamHud(
        teammate=teammate,
        units=(),
        phases=(),
        load=OverloadDegradation.from_orders((), 0, 0),
        forks=(),
        usage=CodexUsage(UnreadableUsage(reason="not requested")),
        packets=(),
        open_pings=(),
    )
    TeamFrame.replace_all(root, TeamPlan(hud.phases), (hud,))
    frame = TeamFrame.at(root, teammate).path
    retired_frame = root / "commander_frames" / frame.name
    retired_frame.parent.mkdir()
    retired_frame.write_text(json.dumps(previous(json.loads(frame.read_text()))))
    frame.unlink()
    step = TeamVocabularyFiles(LiveFilesRequest(root), root / "spill")
    # The backup and the practice copy select from these: on 2026-11-04 they held neither.
    assert {retired, store, retired_frame, frame} <= set(step.input_files())
    # A live directory holds a lock for each document it ever had, including retired teammates'.
    store_lock_path(retired).touch()
    store_lock_path(retired.with_name("retired-teammate.json")).touch()

    carried(TeamVocabularyFiles, root)

    # A failed install still has somewhere to restore the documents to.
    assert (root / "commander_updates").is_dir() and not retired.exists()
    assert step.after_acceptance().changed == 2
    assert TeamUpdateStore.for_team(root, teammate).read() == (packet,)
    # A HUD read finds the teammate's own frame before the heartbeat writes another.
    assert TeamFrame.current(team_bus, "unit-x").teammate == teammate
    assert not (root / "commander_updates").exists() and not (root / "commander_frames").exists()
    carried_passes = services.read()
    assert set(carried_passes.failing) == {"TeamHeartbeat"}
    assert teammate in carried_passes.teammate_completed
