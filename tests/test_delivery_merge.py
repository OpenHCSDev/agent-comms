"""Small record/codec checks; the real CLI proof is recorded separately."""
from agent_comms.field_codec import FieldCodec
from agent_comms.todos import (
    BranchDeliveryMerge, CheckedCommitDelivery, DeliveryMerge, ListedPullRequest,
    MainDeliveryMerge, OrderDelivery, UnmergedDeliveryMerge,
)


def test_delivery_integration_members_keep_their_data_through_the_only_codec():
    for state in (MainDeliveryMerge(), BranchDeliveryMerge('release', 'b' * 40),
                  UnmergedDeliveryMerge(('fix/order',))):
        delivery = CheckedCommitDelivery(commit='a' * 40, delivered_commit='a' * 40, merge=state)
        restored = FieldCodec.decode(OrderDelivery, FieldCodec.encode(delivery))
        assert restored == delivery
        assert FieldCodec.decode(DeliveryMerge, FieldCodec.encode(state)) == state
        assert state.line in restored.line
        assert restored.work == f'delivered commit {"a" * 40}'


def test_named_forge_row_preserves_the_actual_nonmain_merge_target():
    row = FieldCodec.decode(ListedPullRequest, FieldCodec.parse('''{
        "number": 42, "branch": "fix/order", "head": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "merge": {"oid": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"},
        "merged": "2026-10-19T12:00:00Z", "updated": "2026-10-19T12:00:01Z",
        "body": "Agent-Comms-Order: proof-order", "base": "release"
    }'''))
    request = row.parsed()
    assert request.base == 'release'
    assert request.merge == 'b' * 40
    assert request.orders == ('proof-order',)
