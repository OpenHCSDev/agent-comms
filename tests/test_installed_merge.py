"""Merge observation closes the real stored order, without a completion request."""
from agent_comms.repository_name import RepositoryName

import pytest

from agent_comms.repo import GitCommandError, Repo
from test_order_progress import git


def test_failed_git_is_not_a_negative_ancestry_result(tmp_path):
    with pytest.raises(GitCommandError):
        Repo(checkout=tmp_path / 'missing', github=RepositoryName.from_wire('test/repo')).contains('a' * 40)


def test_a_merge_made_after_the_last_fetch_is_fetched_then_answered(tmp_path):
    origin, installed = tmp_path / 'origin', tmp_path / 'installed'
    origin.mkdir()
    git(origin, 'init', '-q', '-b', 'main')
    git(origin, 'commit', '-q', '--allow-empty', '-m', 'base')
    git(tmp_path, 'clone', '-q', str(origin), str(installed))
    git(origin, 'commit', '-q', '--allow-empty', '-m', 'merged later')
    sha = git(origin, 'rev-parse', 'HEAD')
    repo = Repo(checkout=installed, github=RepositoryName.from_wire('test/repo'))

    assert not repo.contains(sha)
    assert repo.contains(sha, 'origin/main')
    assert not repo.contains('b' * 40)
