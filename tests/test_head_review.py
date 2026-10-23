"""A head verdict goes only on the pull request's current head; a moved head posts nothing."""

import subprocess

import pytest

from agent_comms import repository_name
from agent_comms.errors import ToolRefusedError
from agent_comms.head_review import AcceptedHeadReview, PullRequestHead
from agent_comms.repository_name import RepositoryName

CURRENT = "c" * 40


class Gh:
    """Stands in for gh: answers the PR read and records every status post."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, ...]] = []

    def __call__(self, command, **_options):
        read = command[1] == "api" and command[2].endswith("/pulls/7")
        self.posts.extend(() if read else (tuple(command),))
        return subprocess.CompletedProcess(command, 0, f"{CURRENT}\n" if read else "", "")


@pytest.fixture
def gh(monkeypatch) -> Gh:
    stub = Gh()
    monkeypatch.setattr(repository_name.subprocess, "run", stub)
    return stub


def test_a_moved_head_is_refused_and_posts_nothing(gh: Gh) -> None:
    with pytest.raises(ToolRefusedError, match="moved"):
        PullRequestHead(RepositoryName.from_wire("org/repo"), 7, "a" * 9).current()
    assert gh.posts == []


def test_the_current_head_is_posted_in_full(gh: Gh) -> None:
    repo = RepositoryName.from_wire("org/repo")
    head = PullRequestHead(repo, 7, CURRENT[:9]).current()
    AcceptedHeadReview().post(repo, head, "claude-ai", "#7")
    (post,) = gh.posts
    assert f"repos/org/repo/statuses/{CURRENT}" in post
    assert "state=success" in post
