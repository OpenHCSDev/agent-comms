"""Citations use fetched Git objects, not whichever source is on disk."""
from dataclasses import replace
import shutil

import pytest

from source_revision_fixture import SourceRepository
from test_todos import order
from agent_comms.change_report import CitedRevisions, FileLine, MainRevision, PullRequestHeadRevision
from agent_comms.errors import MissingSourceLine
from agent_comms.field_codec import OwnerRequestKey
from agent_comms.goal_actions import OrderGoalAction
from agent_comms.repo import GitCommandError
from agent_comms.owner_requests import OwnerRequestId
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.todos import FixScope, FixShape, LocatedProblemFinding


def test_source_creation_does_not_reuse_a_retired_directory(tmp_path):
    first = SourceRepository.create(tmp_path)
    shutil.rmtree(first.checkout.parent)
    replacement = SourceRepository.create(tmp_path)
    replacement.scope().require_references(replacement.checkout, MainRevision())
    assert replacement.git(replacement.checkout, 'rev-parse', 'HEAD').strip() == replacement.main_commit


def test_main_revision_refuses_branch_lines_and_ignores_dirty_disk(tmp_path):
    source = SourceRepository.create(tmp_path)
    (source.checkout / 'owner.py').write_text('disk-only\n' * 100)
    revision = MainRevision()
    FileLine('owner.py', 2).require_class(source.checkout, revision)
    for missing in (FileLine('owner.py', 4), FileLine('branch.py', 2)):
        with pytest.raises(MissingSourceLine, match='does not exist'):
            missing.require_exists(source.checkout, revision)
    # All citations in one validation share the fetched commit, not a moving ref.
    source.git(source.remote, 'update-ref', 'refs/heads/main', source.branch_commit)
    with pytest.raises(MissingSourceLine):
        FileLine('owner.py', 4).require_exists(source.checkout, revision)
    FileLine('owner.py', 4).require_class(source.checkout, MainRevision())


def test_pull_request_revision_accepts_branch_only_source_without_checkout(tmp_path):
    source = SourceRepository.create(tmp_path)
    (source.checkout / 'branch.py').write_text('')
    revision = PullRequestHeadRevision(source.link)
    location = FileLine('branch.py', 2)
    location.require_exists(source.checkout, revision)
    location.require_class(source.checkout, revision)
    assert source.git(source.checkout, 'rev-parse', 'HEAD').strip() == source.main_commit
    assert source.git(source.checkout, 'rev-parse', 'refs/agent-comms/cited/7').strip() == source.branch_commit
    # An absent remote PR fails its fetch; it is never silently checked at main.
    with pytest.raises(GitCommandError, match='remote ref'):
        location.require_exists(source.checkout,
            PullRequestHeadRevision(replace(source.link, number=404)))


def test_order_derives_its_only_source_revision_from_its_pull_request(tmp_path):
    source = SourceRepository.create(tmp_path / 'cited-source')
    declaration = order(source)
    location = FileLine('branch.py', 2)
    declaration = replace(declaration, owner=location, finding=LocatedProblemFinding(location),
        scope=FixScope(FixShape(parents=(location,), members=())), pull_requests=(source.link,))
    action = OrderGoalAction(
        problem="P1",
        order=declaration,
        request=OwnerRequestId(OwnerRequestKey.mint()),
        review_at=RecordedEventTime(1000),
        commander="lead",
        expected_revision=1,
        assigner=RecordedThreadIncarnation("lead", 1.0),
    )
    action.require_sendable()
    with pytest.raises(MissingSourceLine):
        replace(action, order=replace(declaration, pull_requests=())).require_sendable()


def test_cited_revisions_accept_a_line_from_the_held_head_or_main(tmp_path):
    source = SourceRepository.create(tmp_path)
    revision = CitedRevisions((PullRequestHeadRevision(source.link), MainRevision()))
    for location in (FileLine('branch.py', 2), FileLine('owner.py', 2)):
        location.require_exists(source.checkout, revision)
        location.require_class(source.checkout, revision)
    with pytest.raises(MissingSourceLine, match='does not exist'):
        FileLine('owner.py', 400).require_exists(source.checkout, revision)


def test_a_replacement_order_carries_the_problem_pull_requests_once(tmp_path):
    source = SourceRepository.create(tmp_path / 'cited-source')
    declaration = order(source)
    carried = declaration.carrying((source.link, source.link))
    assert carried.pull_requests == (source.link,)
    assert carried.carrying((source.link,)) is carried
    assert isinstance(carried.cited_revision, CitedRevisions)
