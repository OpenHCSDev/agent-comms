"""Small record/codec checks; the real CLI proof is recorded separately."""
from agent_comms.field_codec import FieldCodec
from agent_comms.todos import (
    BranchDeliveryMerge, CheckedCommitDelivery, DeliveryMerge, ListedPullRequest,
    MainDeliveryMerge, MergedListedPullRequest, MergedPullRequest, OrderDelivery, PullRequestCommit,
    UnmergedDeliveryMerge, UnmergedListedPullRequest, UnmergedPullRequest,
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
    original = MergedListedPullRequest(42, 'fix/order', 'a' * 40,
        updated='2026-10-19T12:00:01Z', body='Agent-Comms-Order: proof-order', base='release',
        merge=PullRequestCommit('b' * 40), merged='2026-10-19T12:00:00Z')
    row = FieldCodec.decode(ListedPullRequest, FieldCodec.encode(original))
    request = row.parsed()
    assert isinstance(request, MergedPullRequest)
    assert request.base == 'release'
    assert request.merge == 'b' * 40
    assert request.merged_at < request.updated_at
    assert request.orders == ('proof-order',)
    assert FieldCodec.decode(ListedPullRequest, FieldCodec.encode(row)) == row


def test_unmerged_query_row_has_no_merge_fields():
    original = UnmergedListedPullRequest(43, 'fix/order', 'a' * 40,
        updated='2026-10-19T12:00:01Z', body='Agent-Comms-Order: proof-order', base='main')
    row = FieldCodec.decode(ListedPullRequest, FieldCodec.encode(original))
    request = row.parsed()
    assert isinstance(request, UnmergedPullRequest)
    assert tuple(request.merged_commits_since(0)) == ()
    assert FieldCodec.decode(ListedPullRequest, FieldCodec.encode(row)) == row
