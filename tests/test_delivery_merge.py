"""Small record/codec checks; the real CLI proof is recorded separately."""
from agent_comms.field_codec import FieldCodec
from agent_comms.todos import (
    BranchDeliveryMerge, CheckedCommitDelivery, DeliveryMerge, DraftListedPullRequest,
    DraftPullRequest, ListedPullRequest,
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
        updated='2026-10-19T12:00:01Z', base='release',
        reviewable_times=('2026-10-19T12:00:00Z',),
        merge=PullRequestCommit('b' * 40), merged='2026-10-19T12:00:00Z')
    row = FieldCodec.decode(ListedPullRequest, FieldCodec.encode(original))
    request = row.parsed((), ('proof-order',))
    assert type(request) is MergedPullRequest
    assert request.base == 'release'
    assert request.merge == 'b' * 40
    assert request.merged_at < request.updated_at
    assert request.orders == ('proof-order',)
    assert FieldCodec.decode(ListedPullRequest, FieldCodec.encode(row)) == row


def test_draft_query_row_preserves_membership_without_review_or_git_reads(tmp_path):
    from agent_comms.work_branches import WorkBranches

    original = DraftListedPullRequest(44, 'fix/draft', 'c' * 40,
        updated='2026-10-19T12:00:01Z', base='main',
        reviewable_times=('2026-10-19T12:00:00Z',))
    row = FieldCodec.decode(ListedPullRequest, FieldCodec.encode(original))
    request = row.parsed(('P664',), ('draft-order',))
    assert request == DraftPullRequest(44, 'fix/draft', 'c' * 40, row.updated_at,
                                     ('draft-order',), problems=('P664',))
    assert row.as_unmerged() == (row,)
    assert row.review_due(float('inf'), 0) == ()
    assert request.head_review_due(float('inf'), 0) == ()
    assert request.as_ready() == ()
    assert request.as_draft() == (request,)
    # This is not a repository: even attempting the diff would fail.
    assert request.empty_changes(WorkBranches(tmp_path / 'not-a-repository')) == ()
    assert FieldCodec.decode(ListedPullRequest, FieldCodec.encode(row)) == row


def test_unmerged_query_row_has_no_merge_fields():
    original = UnmergedListedPullRequest(43, 'fix/order', 'a' * 40,
        updated='2026-10-19T12:00:01Z', base='main',
        reviewable_times=('2026-10-19T12:00:00Z',))
    row = FieldCodec.decode(ListedPullRequest, FieldCodec.encode(original))
    request = row.parsed((), ('proof-order',))
    assert type(request) is UnmergedPullRequest
    assert tuple(request.merged_commits(0)) == ()
    assert FieldCodec.decode(ListedPullRequest, FieldCodec.encode(row)) == row
