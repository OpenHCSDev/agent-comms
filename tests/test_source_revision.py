"""Citations use fetched Git objects, not whichever source is on disk."""
from dataclasses import replace
import shutil

import pytest

from source_revision_fixture import SourceRepository
from test_todos import order
from agent_comms.change_report import FileLine, MainRevision, MissingSourceLine, PullRequestHeadRevision
from agent_comms.field_codec import MalformedRecord
from agent_comms.goal_actions import OrderGoalAction
from agent_comms.goals import GitCommandFailed
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
    with pytest.raises(GitCommandFailed, match='remote ref'):
        with MalformedRecord.refusing('order.owner'):
            location.require_exists(source.checkout,
                PullRequestHeadRevision(replace(source.link, number=404)))


def test_order_derives_its_only_source_revision_from_its_pull_request(tmp_path):
    source = SourceRepository.create(tmp_path / 'cited-source')
    declaration = order(source)
    location = FileLine('branch.py', 2)
    declaration = replace(declaration, owner=location, finding=LocatedProblemFinding(location),
        scope=FixScope(FixShape(parents=(location,), members=())), pull_requests=(source.link,))
    action = OrderGoalAction(problem='P1', order=declaration, request='request',
        review_at=RecordedEventTime(1000), commander='lead', expected_revision=1,
        assigner=RecordedThreadIncarnation('lead', 1.0))
    action.require_sendable()
    with pytest.raises(MissingSourceLine):
        replace(action, order=replace(declaration, pull_requests=())).require_sendable()
