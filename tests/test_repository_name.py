"""Repository identities cross the wire and SQL once, without case-sensitive aliases."""

import pytest

from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.repository_name import RepositoryName
from agent_comms.todos import PullRequests, UnmergedListedPullRequest
from github_fixture import GitHubFixture


def test_repository_identity_hash_and_existing_wire_form():
    first = RepositoryName("OpenHCSDev", "Agent-Comms")
    second = RepositoryName.from_wire("OPENHCSDEV/agent-comms")
    assert first == second
    assert len({first, second}) == 1
    assert FieldCodec.encode(first) == "openhcsdev/agent-comms"
    assert FieldCodec.decode(RepositoryName, FieldCodec.encode(first)) == first


@pytest.mark.parametrize("owner", ["ß" * 100, "İ"])
def test_repository_rejects_names_whose_canonical_wire_cannot_be_decoded(owner):
    with pytest.raises(ValueError):
        RepositoryName(owner, "repo")


@pytest.mark.parametrize("count", (1, 300))
def test_complete_pull_request_query_crosses_real_process_boundary(
    github_cli: GitHubFixture, count: int,
):
    repository = RepositoryName("example", "review")
    github_cli.configure(repository)
    expected = tuple(UnmergedListedPullRequest(
        number=number, branch=f"fix/{number}", head=f"{number:040x}", base="main",
        updated="2099-01-01T00:00:00Z", reviewable_times=(),
    ) for number in range(1, count + 1))
    for row in expected:
        github_cli.put(repository, row)

    # The 300-row query exceeds Linux's single exec argument limit. The real
    # foreign process must receive every selection and return every typed row.
    actual = PullRequests().query(repository, tuple(row.number for row in expected))
    assert actual == expected


def test_graphql_variables_cross_the_foreign_boundary(github_cli: GitHubFixture):
    repository = RepositoryName("example", "variables")
    github_cli.configure(repository)
    row = UnmergedListedPullRequest(
        number=7, branch="fix/seven", head="a" * 40, base="main",
        updated="2099-01-01T00:00:00Z", reviewable_times=(),
    )
    github_cli.put(repository, row)
    query = """query($owner: String!, $name: String!, $after: String) {
      repository(owner: $owner, name: $name) {
        pullRequests(first: 100, after: $after) { nodes { number headRefName } }
      }
    }"""
    result = repository.graphql(query, owner=repository.owner, name=repository.name,
                                after="cursor-with-=+&-characters")
    assert result.returncode == 0, result.stderr
    response = FieldCodec.parse(result.stdout)
    assert response == {"data": {"repository": {"pullRequests": {
        "nodes": [{"number": 7, "headRefName": "fix/seven", "isCrossRepository": False}],
        "pageInfo": {"hasNextPage": False, "endCursor": None},
    }}}}

