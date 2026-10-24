"""The context carry unboxes stored identities once, before the normal reader."""

from cutover.carry_context_turn_ids import ContextTurnIds

from agent_comms.context_store import ContextRequest, ContextStore, ContextStoreInfo
from agent_comms.field_codec import FieldCodec, TurnId
from agent_comms.installer.probes.carry import Carried
from agent_comms.thread_identity import RecordedThreadIncarnation, TurnIdentity
from agent_comms.turn_context import RecordedContextIdentity, RecordedContextTurn


def test_context_turn_ids_unboxes_stored_identity_once(tmp_path):
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    store = ContextStore(root)
    turn_id = TurnId.mint()
    owner = RecordedThreadIncarnation("owner", 1.0)
    turn = RecordedContextTurn(turn_id, TurnIdentity(owner, 1))
    identity = RecordedContextIdentity(owner, turn, "pi.estimateTokens")
    old = FieldCodec.encode(identity)
    old["turn"]["identity"] = {"value": FieldCodec.encode(turn_id)}
    table = ContextRequest
    with store.writing() as db:
        ContextStoreInfo.source_in(db).initialized(db)
        db.execute(
            table._sql(
                f'INSERT INTO "{table.declared_name}" ({{identity}}, {{versions}}, {{size}}) '
                "VALUES (?, ?, ?)"
            ),
            (FieldCodec.compact(old), FieldCodec.compact(()), 0),
        )

    step = ContextTurnIds(root, root, tmp_path / "spill")
    assert step.carry(apply=False) == Carried(1)
    carried = step.carry(apply=True)
    assert carried.changed == 1
    assert carried.rewritten == (ContextStore.path_in(root).relative_to(root),)
    with store.snapshot() as db:
        recorded = ContextRequest.require(db, id=1).identity.turn.require_recorded()
        assert recorded == turn
        assert recorded.identity == turn_id
    assert step.carry(apply=True) == Carried(0)
