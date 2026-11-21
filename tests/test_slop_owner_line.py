"""A slop citation's owner is either a line on main or a line the order itself creates."""
import asyncio
import json
from dataclasses import replace

import pytest

from agent_comms.errors import TodoError
from agent_comms.installer.build_identity import RecordedBuildIdentity
from agent_comms.nominal.field_codec import FieldCodec, MalformedRecord
from agent_comms.planning.orders import (
    CommitDelivery,
    MergedListedPullRequest,
)
from agent_comms.planning.progress_claims import MergedClaim, ProblemScope
from agent_comms.planning.pull_requests import (
    PullRequestCommit,
    PullRequestLink,
)
from agent_comms.projects.change_report import FileLine
from agent_comms.projects.checkouts import Checkout
from agent_comms.projects.slop import (
    CreatedLine,
    DeclarationDerivation,
    ExistingLine,
    OwnerLine,
    Slop,
)
from agent_comms.projects.work_branches import WorkBranches
from agent_comms.registry.threads import ForkThread, NativeThread
from agent_comms.team.order import OrderFile
from github_fixture import GitHubFixture
from test_order_scopes import assigned
from test_todos import order

CREATED = FileLine('branch.py', 2)


@pytest.mark.parametrize('wire, owner', (
    ('owner.py:2', ExistingLine(FileLine('owner.py', 2))),
    ({'created': 'branch.py:2'}, CreatedLine(CREATED)),
))
def test_the_boundary_picks_the_member_once_and_encodes_it_back(wire, owner):
    assert FieldCodec.decode(OwnerLine, wire) == owner
    assert FieldCodec.encode(owner) == wire


@pytest.mark.parametrize('wire', ({'created': 'branch.py:2', 'kind': 'x'}, 7))
def test_any_other_owner_shape_is_refused(wire):
    with pytest.raises(MalformedRecord):
        FieldCodec.decode(OwnerLine, wire)


def test_each_member_owns_its_send_and_done_lines():
    existing, created = ExistingLine(FileLine('owner.py', 2)), CreatedLine(CREATED)
    assert (existing.cited(), existing.owed()) == ((existing.line,), ())
    assert (created.cited(), created.owed()) == ((), (CREATED,))


def test_the_order_file_derives_what_the_order_creates_from_its_citations(source_repository):
    slop = Slop('IDEN-5', 'the shape of one branch declaration', FileLine('owner.py', 1),
                DeclarationDerivation, CreatedLine(CREATED))
    document = OrderFile(order=order(source_repository), slop=(slop,),
                         defect='The branch declaration is restated outside its owner file',
                         why_stupid='This is stupid because two files declare one shape')
    read = OrderFile.read(json.dumps(FieldCodec.encode(document)))
    assert read.order.created == (CREATED,)


def test_done_is_refused_until_the_delivered_tree_declares_the_created_line(
        comms, source_repository, github_cli: GitHubFixture, interpreter_build):
    _, current = assigned(comms, source_repository.scope(), source_repository)
    todo = replace(current, order=replace(current.order, created=(CREATED,),
                                          pull_requests=(PullRequestLink(current.repo, 1),)))
    github_cli.configure(todo.repo)

    def verified(commit: str):
        github_cli.put(todo.repo, MergedListedPullRequest(
            1, 'fix/created', commit, updated='2026-10-25T12:00:00Z', base=WorkBranches.main,
            reviewable_times=('2026-10-25T12:00:00Z',), merge=PullRequestCommit(commit),
            merged='2026-10-25T12:00:00Z'))
        delivery = CommitDelivery(commit=commit, delivered_commit=commit,
                                  proof_build=RecordedBuildIdentity(commits=(), interpreter=interpreter_build))
        claims = {MergedClaim(commit, ProblemScope(todo.id)): (WorkBranches.main,)}
        return asyncio.run(delivery.verified(todo, comms.registry, claims))

    with pytest.raises(TodoError, match='branch.py:2'):
        verified(source_repository.main_commit)
    assert verified(source_repository.branch_commit).commit == source_repository.branch_commit


def test_a_fork_is_never_a_teammate_that_could_take_an_order(tmp_path):
    teammate = NativeThread('worker', frozenset(), Checkout(tmp_path))
    fork = NativeThread('shape-next', frozenset(), Checkout(tmp_path)).as_member(ForkThread)
    assert teammate.order_candidates() == (teammate,)
    assert fork.order_candidates() == ()


def test_a_merged_claim_resolves_in_the_problems_checkout_not_the_claimants_worktree(
        comms, source_repository, github_cli: GitHubFixture):
    """A goal report used to verify this claim in the claimant's own worktree, which holds no
    other repository's history, so the same claim could never resolve from a goal."""
    from agent_comms.planning.progress_claims import ProgressClaim, ThreadScope

    _, current = assigned(comms, source_repository.scope(), source_repository)
    commit = source_repository.main_commit
    github_cli.configure(source_repository.github)
    github_cli.put(source_repository.github, MergedListedPullRequest(
        1, 'fix/created', commit, updated='2026-10-25T12:00:00Z', base=WorkBranches.main,
        reviewable_times=('2026-10-25T12:00:00Z',), merge=PullRequestCommit(commit),
        merged='2026-10-25T12:00:00Z'))
    claimant = comms.registry.require('worker')
    assert claimant.worktree.path != current.order.check.directory  # the bug's whole shape

    claim = MergedClaim(commit, ProblemScope(current.id))
    assert WorkBranches.main in ProgressClaim.require_all((claim,), comms, claimant)[claim]

    # A thread-scoped claim is looked for in the claimant's own worktree, which holds none of
    # that repository's history; the lookup fails there loudly instead of finding the commit
    # in someone else's checkout.
    from agent_comms.projects.git_errors import FailedGitCommand

    elsewhere = MergedClaim(commit, ThreadScope())
    with pytest.raises(FailedGitCommand, match='not a git repository'):
        ProgressClaim.require_all((elsewhere,), comms, claimant)


def test_a_delivery_answers_integration_and_completion_as_two_separate_facts(
        source_repository, interpreter_build):
    """`merged_commits(repo)` is the integration question, asked about a repository;
    `claim_commits()` is the completion question, which needs none. One no-argument override
    under the integration name shadowed the Repo-taking one through the MRO, so the ordinary
    CommitDelivery path raised instead of answering."""
    commit = source_repository.main_commit
    delivery = CommitDelivery(commit=commit, delivered_commit=commit,
                              proof_build=RecordedBuildIdentity(commits=(), interpreter=interpreter_build))
    assert delivery.claim_commits() == (commit,)
    assert delivery.merged_commits(source_repository) == (commit,)
