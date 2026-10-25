"""Citations use fetched Git objects, not whichever source is on disk."""
from dataclasses import replace
import shutil

import pytest

from source_revision_fixture import SourceRepository
from test_todos import order
from agent_comms.change_report import (
    CitedRevisions, FileLine, MainRevision, OrderRoots, PullRequestHeadRevision, RegisteredRoots,
)
from agent_comms.repository_name import RepositoryName
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
    replacement.scope().require_references(replacement.roots, MainRevision())
    assert replacement.git(replacement.checkout, 'rev-parse', 'HEAD').strip() == replacement.main_commit


def test_main_revision_refuses_branch_lines_and_ignores_dirty_disk(tmp_path):
    source = SourceRepository.create(tmp_path)
    (source.checkout / 'owner.py').write_text('disk-only\n' * 100)
    revision = MainRevision()
    FileLine('owner.py', 2).require_class(source.roots, revision)
    for missing in (FileLine('owner.py', 4), FileLine('branch.py', 2)):
        with pytest.raises(MissingSourceLine, match='does not exist'):
            missing.require_exists(source.roots, revision)
    # All citations in one validation share the fetched commit, not a moving ref.
    source.git(source.remote, 'update-ref', 'refs/heads/main', source.branch_commit)
    with pytest.raises(MissingSourceLine):
        FileLine('owner.py', 4).require_exists(source.roots, revision)
    FileLine('owner.py', 4).require_class(source.roots, MainRevision())


def test_pull_request_revision_accepts_branch_only_source_without_checkout(tmp_path):
    source = SourceRepository.create(tmp_path)
    (source.checkout / 'branch.py').write_text('')
    revision = PullRequestHeadRevision(source.link)
    location = FileLine('branch.py', 2)
    location.require_exists(source.roots, revision)
    location.require_class(source.roots, revision)
    assert source.git(source.checkout, 'rev-parse', 'HEAD').strip() == source.main_commit
    assert source.git(source.checkout, 'rev-parse', 'refs/agent-comms/cited/7').strip() == source.branch_commit
    # An absent remote PR fails its fetch; it is never silently checked at main.
    with pytest.raises(GitCommandError, match='remote ref'):
        location.require_exists(source.roots,
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
    action.require_sendable(source.project)
    with pytest.raises(MissingSourceLine):
        replace(action, order=replace(declaration, pull_requests=())).require_sendable(source.project)


def test_cited_revisions_accept_a_line_from_the_held_head_or_main(tmp_path):
    source = SourceRepository.create(tmp_path)
    revision = CitedRevisions((PullRequestHeadRevision(source.link), MainRevision()))
    for location in (FileLine('branch.py', 2), FileLine('owner.py', 2)):
        location.require_exists(source.roots, revision)
        location.require_class(source.roots, revision)
    with pytest.raises(MissingSourceLine, match='does not exist'):
        FileLine('owner.py', 400).require_exists(source.roots, revision)


def test_a_replacement_order_carries_the_problem_pull_requests_once(tmp_path):
    source = SourceRepository.create(tmp_path / 'cited-source')
    declaration = order(source)
    carried = declaration.carrying((source.link, source.link))
    assert carried.pull_requests == (source.link,)
    assert carried.carrying((source.link,)) is carried
    assert isinstance(carried.cited_revision, CitedRevisions)


def test_a_citation_names_its_repository_only_when_it_is_registered(tmp_path):
    source = SourceRepository.create(tmp_path / 'order')
    other = SourceRepository.create(tmp_path / 'other', RepositoryName.from_wire('example/other'))
    both = OrderRoots(source.checkout, replace(source.project, repos=(source, other)))
    cited = FileLine.from_wire('example/other:owner.py:2')
    assert cited.to_wire() == 'example/other:owner.py:2'
    # In prose a registered prefix is the repository; a revision prefix is not part of the citation.
    assert FileLine.scan('owner at Example/Other:owner.py:2, shown by origin/main:branch.py:2 '
                         'and HEAD~1:branch.py:3', both.project) == (
        cited, FileLine('branch.py', 2), FileLine('branch.py', 3))
    # The order's pull request belongs to the order's repository; the other repository's
    # main still answers the citation.
    revision = CitedRevisions((PullRequestHeadRevision(source.link), MainRevision()))
    cited.require_class(both, revision)
    with pytest.raises(MissingSourceLine, match='does not exist'):
        FileLine.from_wire('example/other:branch.py:2').require_exists(both, revision)
    # A declared field stays strict: an unregistered repository is refused with its install step.
    with pytest.raises(ValueError, match='agent-comms install --repos'):
        cited.require_exists(source.roots, revision)
    # With no order behind it, an unqualified citation needs a single registered repository.
    FileLine('owner.py', 2).require_exists(RegisteredRoots(source.project), MainRevision())
    with pytest.raises(ValueError, match='cite it as owner/name:FILE:LINE'):
        FileLine('owner.py', 2).require_exists(RegisteredRoots(both.project), MainRevision())
    cited.require_exists(RegisteredRoots(both.project), MainRevision())
