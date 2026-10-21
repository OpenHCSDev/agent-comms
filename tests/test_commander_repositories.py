"""Only the machine's declared repositories supply order-check directories."""
from pathlib import Path

import pytest

from agent_comms.commander.config import CommanderConfig
from agent_comms.repo import Repo
from agent_comms.repository_name import RepositoryName


@pytest.fixture
def configured_repositories(tmp_path: Path) -> CommanderConfig:
    repos = tuple(
        Repo(checkout=tmp_path / name, github=RepositoryName.from_wire(f"example/{name}"))
        for name in ("agent-comms", "toad")
    )
    for repo in repos:
        repo.checkout.mkdir()
    return CommanderConfig.on_machine("lead", repos, tmp_path / "bus", tmp_path)


def test_configured_name_and_checkout_select_the_same_repository(configured_repositories):
    config = configured_repositories
    for repo in config.repos:
        assert config.repo_directory(repo.github.name) == repo.checkout
        assert config.repo_directory(str(repo.checkout)) == repo.checkout


def test_existing_unconfigured_worktree_is_refused_with_configured_choices(
    configured_repositories, tmp_path,
):
    config = configured_repositories
    worktree = tmp_path / "fork-records--P408"
    worktree.mkdir()
    with pytest.raises(ValueError) as refusal:
        config.repo_directory(str(worktree))
    message = str(refusal.value)
    assert str(worktree) in message
    for repo in config.repos:
        assert repo.github.name in message
        assert str(repo.checkout) in message
    assert "agent-comms install" in message and "--repos" in message
