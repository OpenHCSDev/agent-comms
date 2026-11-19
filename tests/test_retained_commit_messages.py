"""Fresh message evidence changes only its own postings; old cuts remain answers."""
import re
from pathlib import Path
from unittest.mock import patch

import pytest

import fixture_git
from agent_comms.projects.checkouts import Checkout, CommitMessages, CommitsNaming, GitCommit, RepositoryReads
from agent_comms.projects.repo import Repo


def records(*texts):
    return tuple(GitCommit(str(index), (), float(index), text) for index, text in enumerate(texts))


class ObservedWords:
    def __init__(self, original):
        self.original = original
        self.read = []

    def findall(self, text):
        self.read.append(text)
        return self.original.findall(text)

    def fullmatch(self, text):
        return self.original.fullmatch(text)


@pytest.mark.parametrize('after', [
    records('P1 stable', 'new words'),
    records('P1 stable'),
    records('P1 stable', 'P3 changed same SHA', 'P4 new'),
    records('P1 stable', 'P2 old'),
    (),
])
def test_retained_postings_change_only_changed_original_messages(after):
    original = records('P1 stable', 'P2 old')
    before = CommitMessages(original)
    before_words = before.word_names
    with pytest.raises(TypeError):
        before.messages_by_sha['0'] = 'forged source'
    observed = ObservedWords(CommitMessages.words)
    with patch.object(CommitMessages, 'words', observed):
        following = before.following(after)
        following_words = following.word_names
    previous = {record.sha: record.message.lower() for record in original}
    current = {record.sha: record.message.lower() for record in after}
    changed = {sha for sha in previous.keys() | current.keys() if previous.get(sha) != current.get(sha)}
    expected_reads = [message for source in (previous, current) for sha, message in source.items() if sha in changed]
    assert sorted(observed.read) == sorted(expected_reads)
    for token in ('p1', 'p2', 'p3', 'p4', 'stable', 'new words', 'changed same'):
        expected = frozenset(record.sha for record in after if re.search(
            r'(?<![a-z0-9_])' + re.escape(token) + r'(?![a-z0-9_])', record.message.lower()))
        assert following.naming(token) == expected
    assert before.naming('P2') == frozenset({'1'})
    assert before.word_names is before_words
    if 'stable' in following_words:
        assert following_words['stable'] is before_words['stable']
    with pytest.raises(TypeError):
        following_words['injected'] = frozenset()


@pytest.mark.parametrize('token', ['hello\nworld', 'hello\n', '\nworld', ' ', '', 'É'])
def test_phrase_boundaries_are_inside_each_original_message(token):
    commits = records('hello', 'world', ' É ')
    expected = frozenset(record.sha for record in commits if re.search(
        r'(?<![a-z0-9_])' + re.escape(token.lower()) + r'(?![a-z0-9_])', record.message.lower()))
    assert CommitMessages(commits).naming(token) == expected


def test_real_changed_ref_keeps_old_cut_and_reuses_unchanged_postings(tmp_path: Path):
    git = fixture_git.run
    main = tmp_path / 'main'
    main.mkdir()
    git(main, 'init', '--initial-branch=main')
    git(main, 'commit', '--allow-empty', '-m', 'P1 original')
    reads = RepositoryReads()
    earlier = reads.history_for(Checkout(main))
    old_answer = tuple(earlier.commits(('main',), selected=CommitsNaming('P1')))
    old_words = earlier.messages.word_names
    git(main, 'commit', '--allow-empty', '-m', 'P2 added')
    following = RepositoryReads.retaining(reads).history_for(Checkout(main))
    assert tuple(following.commits(('main',), selected=CommitsNaming('P1'))) == old_answer
    assert len(tuple(following.commits(('main',), selected=CommitsNaming('P2')))) == 1
    assert following.messages.word_names['p1'] is old_words['p1']
    assert tuple(earlier.commits(('main',), selected=CommitsNaming('P2'))) == ()


def test_removed_history_drops_postings_without_changing_original_cut(tmp_path: Path):
    git = fixture_git.run
    main = tmp_path / 'main'
    main.mkdir()
    git(main, 'init', '--initial-branch=main')
    git(main, 'commit', '--allow-empty', '-m', 'P1 root')
    git(main, 'checkout', '-b', 'temporary')
    git(main, 'commit', '--allow-empty', '-m', 'P3 temporary')
    reads = RepositoryReads()
    earlier = reads.history_for(Checkout(main))
    original = tuple(earlier.commits(('temporary',), selected=CommitsNaming('P3')))
    git(main, 'checkout', 'main')
    git(main, 'branch', '-D', 'temporary')
    following = RepositoryReads.retaining(reads).history_for(Checkout(main))
    assert following.messages.naming('P3') == frozenset()
    assert tuple(earlier.commits(('temporary',), selected=CommitsNaming('P3'))) == original
    assert following.messages.word_names['p1'] is earlier.messages.word_names['p1']


def test_retention_does_not_acquire_new_graph_or_an_unused_word_index(tmp_path: Path):
    git = fixture_git.run
    main = tmp_path / 'main'
    main.mkdir()
    git(main, 'init', '--initial-branch=main')
    git(main, 'commit', '--allow-empty', '-m', 'word-only root')
    reads = RepositoryReads()
    earlier = reads.history_for(Checkout(main))
    earlier.graph
    assert earlier.messages.naming('word-only')
    git(main, 'commit', '--allow-empty', '-m', 'new root')
    original_git = Repo.git

    def without_graph(*args, **kwargs):
        assert 'rev-list' not in args
        return original_git(*args, **kwargs)

    observed = ObservedWords(CommitMessages.words)
    with patch.object(Repo, 'git', without_graph), patch.object(CommitMessages, 'words', observed):
        following = RepositoryReads.retaining(reads).history_for(Checkout(main))
        assert following is not earlier
    assert observed.read == []
