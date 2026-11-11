"""A delivery follows its checked repository, not the problem's home repository."""

import asyncio
from dataclasses import replace
from typing import get_type_hints

import pytest

from agent_comms.errors import TodoError
from agent_comms.planning.delivery_evidence import NoDeliveryEvidence
from agent_comms.planning.orders import (
    MergedListedPullRequest,
    PullRequests,
)
from agent_comms.planning.pull_requests import (
    MainDeliveryMerge,
    PullRequestCommit,
    PullRequestLink,
)
from agent_comms.projects.repository_name import RepositoryName
from agent_comms.projects.work_branches import WorkBranches
from agent_comms.team.order import Team
from github_fixture import GitHubFixture
from test_held_problem_orders import merge_two_commit_fix
from test_todos import adopt_pr, pr_order

pytest_plugins = ("test_todos",)
OTHER_REPOSITORY = RepositoryName.from_wire("example/client")


@pytest.mark.parametrize(("home_is_source", "source_linked"), [
    (True, True),
    (False, True),
    (False, False),
])
def test_delivery_resolves_only_the_linked_checked_repository(
    pr_team: Team, github_cli: GitHubFixture, home_is_source: bool, source_linked: bool,
) -> None:
    get_type_hints(PullRequests.merged_delivery)
    source = pr_team.repos[0]
    home = source.github if home_is_source else OTHER_REPOSITORY
    number = 44
    merge_two_commit_fix(source, github_cli.directory, number)
    original = adopt_pr(pr_team, pr_order(pr_team, "P1").id, number)
    expected, = PullRequests((original,)).listed(source.github, MergedListedPullRequest, original)
    links = (PullRequestLink(home, number),)
    if source_linked:
        links = tuple(dict.fromkeys((*links, PullRequestLink(source.github, number))))
    # These are immutable reader inputs, reachable through TodoStore.create and PR linking;
    # no canonical row or private reader cache is changed by this case matrix.
    current = replace(original, repo=home, order=replace(original.order, pull_requests=links))
    if home != source.github:
        github_cli.configure(home)
        github_cli.put(home, MergedListedPullRequest(
            number=number, branch="feature/other", head="e" * 40,
            updated="2099-01-01T00:00:00Z", base=WorkBranches.main,
            reviewable_times=("2099-01-01T00:00:00Z",),
            merge=PullRequestCommit("f" * 40), merged="2099-01-01T00:00:00Z",
        ))
    reader = PullRequests((current,))
    if not source_linked:
        with pytest.raises(TodoError, match="has no merged pull request"):
            reader.merged_delivery(current, source.checkout, NoDeliveryEvidence())
    else:
        delivered = reader.merged_delivery(current, source.checkout, NoDeliveryEvidence())
        assert (delivered.commit, delivered.delivered) == (expected.merge, expected.head)
        assert delivered.commit != delivered.delivered
        merge = asyncio.run(delivered.merge_state(current, pr_team.comms.registry, (WorkBranches.main,)))
        assert merge == MainDeliveryMerge()
    assert current.repository.github == source.github
    assert (current.repo, original.repo) == (home, source.github)
