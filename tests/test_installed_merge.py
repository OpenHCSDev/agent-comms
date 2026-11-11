"""Ancestry checks never fetch, and a failed git command is never a negative answer."""
import pytest

from agent_comms.projects.git_errors import (
    GitCommandError,
    UnavailableCommitError,
)
from agent_comms.projects.repo import Repo
from agent_comms.projects.repository_name import RepositoryName
from test_order_progress import git


def test_failed_git_is_not_a_negative_ancestry_result(tmp_path):
    with pytest.raises(GitCommandError):
        Repo(checkout=tmp_path / 'missing', github=RepositoryName.from_wire('test/repo')).contains('a' * 40)


def test_merge_preparation_acquires_objects_but_contains_never_fetches(tmp_path):
    origin, installed = tmp_path / 'origin', tmp_path / 'installed'
    origin.mkdir()
    git(origin, 'init', '-q', '-b', 'main')
    git(origin, 'commit', '-q', '--allow-empty', '-m', 'base')
    git(tmp_path, 'clone', '-q', str(origin), str(installed))
    git(origin, 'commit', '-q', '--allow-empty', '-m', 'merged later')
    sha = git(origin, 'rev-parse', 'HEAD')
    repo = Repo(checkout=installed, github=RepositoryName.from_wire('test/repo'))

    with pytest.raises(UnavailableCommitError, match=sha):
        repo.contains(sha)
    Repo.require_commits(installed, 'origin', sha)
    assert not repo.contains(sha)
    assert repo.contains(sha, sha)
    # Acquisition does not refresh refs; that is a separate explicit command effect.
    assert not repo.contains(sha, 'origin/main')
    with pytest.raises(UnavailableCommitError, match='b' * 40):
        repo.contains('b' * 40)
