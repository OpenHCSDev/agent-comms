"""One pass's repository cut against worktrees that appear or vanish while it reads."""
import subprocess
import re
import pickle
from itertools import product
from operator import setitem
from pathlib import Path

import pytest

import fixture_git
from agent_comms.projects.checkouts import (
    Checkout,
    CommitMessages,
    CommitsNaming,
    GitCommit,
    RepositoryReads,
)
from agent_comms.projects.git_errors import FailedGitCommand

git = fixture_git.run


@pytest.fixture
def main(tmp_path: Path) -> Path:
    path = tmp_path / "main"
    path.mkdir()
    git(path, "init", "--initial-branch=main")
    git(path, "commit", "--allow-empty", "-m", "first")
    return path


def test_a_worktree_added_after_the_cut_answers_its_own_head(main: Path) -> None:
    reads = RepositoryReads()
    reads.history_for(Checkout(main))
    added = main.parent / "added"
    git(main, "worktree", "add", "-b", "later", str(added))
    assert reads.history_for(Checkout(added)).head_for(Checkout(added)).required() == (git(added, "rev-parse", "HEAD"),)
    assert reads.history_for(Checkout(main)).head_for(Checkout(main)).required() == (git(main, "rev-parse", "HEAD"),)


def test_a_listed_worktree_removed_after_the_cut_keeps_its_cut_head(main: Path) -> None:
    listed = main.parent / "listed"
    git(main, "worktree", "add", "-b", "soon-gone", str(listed))
    head = git(listed, "rev-parse", "HEAD")
    reads = RepositoryReads()
    reads.history_for(Checkout(main))
    resolved = Checkout(listed).resolved()
    git(main, "worktree", "remove", str(listed))
    assert reads.history_for(resolved).head_for(resolved).required() == (head,)


def test_a_worktree_gone_before_the_pass_reads_it_refuses_by_its_git_error(main: Path) -> None:
    gone = main.parent / "gone"
    git(main, "worktree", "add", "-b", "never-read", str(gone))
    reads = RepositoryReads()
    git(main, "worktree", "remove", str(gone))
    with pytest.raises(FailedGitCommand):
        reads.history_for(Checkout(gone))


@pytest.mark.parametrize("change", ("loose", "deleted", "detached", "worktree", "unchanged"))
def test_retention_checks_refs_and_worktree_heads(main: Path, change: str) -> None:
    git(main, "branch", "retirable")
    git(main, "pack-refs", "--all", "--prune")
    reads = RepositoryReads()
    original = reads.history_for(Checkout(main))
    original.graph
    reads = RepositoryReads.retaining(reads)
    before_refs, before_heads = dict(original.refs), dict(original.heads)
    if change == "loose":
        git(main, "commit", "--allow-empty", "-m", "loose ref moved")
    elif change == "deleted":
        # A loose-only ref disappears without touching packed-refs.
        git(main, "branch", "loose-only")
        reads = RepositoryReads()
        original = reads.history_for(Checkout(main))
        original.graph
        reads = RepositoryReads.retaining(reads)
        before_refs, before_heads = dict(original.refs), dict(original.heads)
        git(main, "branch", "-D", "loose-only")
    elif change == "detached":
        git(main, "checkout", "--detach", "-q")
        git(main, "commit", "--allow-empty", "-m", "detached moved")
    elif change == "worktree":
        git(main, "worktree", "add", "--detach", str(main.parent / "later-cut"))
    following = RepositoryReads.retaining(reads).history_for(Checkout(main))
    actual = Checkout(main).read()
    assert following.refs == actual.refs
    assert following.heads == actual.heads
    assert original.refs == before_refs and original.heads == before_heads
    if change == "unchanged":
        assert following is original
    else:
        assert following.refs != before_refs or following.heads != before_heads


def test_retaining_reloads_changed_git_replacement_policy(main: Path) -> None:
    git(main, 'commit', '--allow-empty', '-m', 'P303 original')
    original = git(main, 'rev-parse', 'HEAD')
    replacement = git(main, 'commit-tree', 'HEAD^{tree}', '-m', 'P304 replacement')
    reads = RepositoryReads()
    previous = reads.history_for(Checkout(main))
    before = dict(previous.graph)
    assert previous.messages.naming('P303') == {original}
    git(main, 'replace', original, replacement)
    following_reads = RepositoryReads.retaining(reads)
    following = following_reads.history_for(Checkout(main))
    assert following.refs == previous.refs and following.heads == previous.heads
    assert following.graph == Checkout(main).read().graph
    assert following.graph[original].parents == ()
    assert following.messages.naming('P304') == {original}
    assert previous.graph == before and previous.messages.naming('P303') == {original}
    git(main, 'pack-refs', '--all')
    assert RepositoryReads.retaining(following_reads).history_for(Checkout(main)) is following
    git(main, 'replace', '-d', original)
    restored = RepositoryReads.retaining(following_reads).history_for(Checkout(main))
    assert restored.graph == before
    assert following.messages.naming('P304') == {original}


def test_retaining_reloads_changed_shallow_boundary(main: Path) -> None:
    git(main, 'commit', '--allow-empty', '-m', 'P303 shallow tip')
    tip = git(main, 'rev-parse', 'HEAD')
    shallow = Path(git(main, 'rev-parse', '--path-format=absolute', '--git-path', 'shallow'))
    reads = RepositoryReads()
    previous = reads.history_for(Checkout(main))
    before = dict(previous.graph)
    shallow.write_text(tip + '\n')
    following_reads = RepositoryReads.retaining(reads)
    following = following_reads.history_for(Checkout(main))
    assert following.refs == previous.refs and following.heads == previous.heads
    assert following.graph == Checkout(main).read().graph
    assert tuple(following.graph) == (tip,)
    assert previous.graph == before
    shallow.unlink()
    restored = RepositoryReads.retaining(following_reads).history_for(Checkout(main))
    assert restored.graph == before
    assert tuple(following.graph) == (tip,)
    assert dict(following.policy.boundary_files)[shallow].content == (tip + "\n").encode()


def test_retaining_reloads_changed_graft_boundary(main: Path) -> None:
    git(main, 'commit', '--allow-empty', '-m', 'P303 graft tip')
    tip = git(main, 'rev-parse', 'HEAD')
    graft = Path(git(main, 'rev-parse', '--path-format=absolute', '--git-path', 'info/grafts'))
    reads = RepositoryReads()
    previous = reads.history_for(Checkout(main))
    before = dict(previous.graph)
    graft.write_text(tip + '\n')
    following_reads = RepositoryReads.retaining(reads)
    following = following_reads.history_for(Checkout(main))
    assert following.refs == previous.refs and following.heads == previous.heads
    assert following.graph == Checkout(main).read().graph
    assert tuple(following.graph) == (tip,)
    assert previous.graph == before
    graft.unlink()
    restored = RepositoryReads.retaining(following_reads).history_for(Checkout(main))
    assert restored.graph == before
    assert tuple(following.graph) == (tip,)
    assert dict(following.policy.boundary_files)[graft].content == (tip + "\n").encode()


def test_retaining_reloads_changed_replacement_setting(main: Path) -> None:
    git(main, 'commit', '--allow-empty', '-m', 'P303 original')
    original = git(main, 'rev-parse', 'HEAD')
    replacement = git(main, 'commit-tree', 'HEAD^{tree}', '-m', 'P304 replacement')
    git(main, 'replace', original, replacement)
    reads = RepositoryReads()
    previous = reads.history_for(Checkout(main))
    before = dict(previous.graph)
    assert previous.messages.naming('P304') == {original}
    git(main, 'config', 'core.useReplaceRefs', 'false')
    following_reads = RepositoryReads.retaining(reads)
    following = following_reads.history_for(Checkout(main))
    assert following.refs == previous.refs and following.heads == previous.heads
    assert following.graph == Checkout(main).read().graph
    assert following.messages.naming('P303') == {original}
    assert previous.graph == before
    git(main, 'config', 'core.useReplaceRefs', 'true')
    restored = RepositoryReads.retaining(following_reads).history_for(Checkout(main))
    assert restored.graph == before
    assert following.messages.naming('P303') == {original}


def test_changed_invalid_policy_reports_its_source_failure(main: Path) -> None:
    reads = RepositoryReads()
    previous = reads.history_for(Checkout(main))
    before = dict(previous.graph)
    policy = previous.policy
    git(main, 'config', 'core.useReplaceRefs', 'not-a-boolean')
    with pytest.raises(FailedGitCommand, match='not-a-boolean'):
        following = RepositoryReads.retaining(reads).history_for(Checkout(main))
        following.graph
    assert previous.graph == before and previous.policy is policy


@pytest.mark.parametrize('name', ('shallow', 'info/grafts'))
def test_unreadable_boundary_reports_its_original_file(main: Path, name: str) -> None:
    reads = RepositoryReads()
    previous = reads.history_for(Checkout(main))
    before = dict(previous.graph)
    boundary = Path(git(main, 'rev-parse', '--path-format=absolute', '--git-path', name))
    boundary.mkdir()
    following = RepositoryReads.retaining(reads).history_for(Checkout(main))
    with pytest.raises(IsADirectoryError) as raised:
        following.graph
    assert raised.value.filename == str(boundary)
    assert previous.graph == before


@pytest.mark.parametrize('read', (
    lambda history: history.refs,
    lambda history: history.heads,
    lambda history: history.graph,
))
def test_captured_history_maps_keep_order_and_reject_reader_mutation(main: Path, read) -> None:
    reads = RepositoryReads()
    history = reads.history_for(Checkout(main))
    history.graph
    captured = read(history)
    key, value = next(iter(captured.items()))
    with pytest.raises(TypeError):
        setitem(captured, key, value)
    restored = pickle.loads(pickle.dumps(captured))
    assert list(restored.items()) == list(captured.items())
    with pytest.raises(TypeError):
        setitem(restored, key, value)
    assert RepositoryReads.retaining(reads).history_for(Checkout(main)) is history


def test_captured_graph_owns_its_supplied_mapping(main: Path) -> None:
    original = Checkout(main).read().graph
    supplied = dict(original)
    captured = type(original)(original.source, supplied)
    supplied.clear()
    assert tuple(captured.items()) == tuple(original.items())
    assert captured.source is original.source
    with pytest.raises(TypeError):
        captured.commits['injected'] = next(iter(original.values()))


def test_indexed_commit_words_and_literal_phrases_keep_the_boundary_contract():
    texts = ("P1 first; XP1 P1x _P1 P12", "p1\nrefs P3", "ÉP1é; İ P1-P3", "punctuation-only !",
             "a_1 -- a a-a a\n1", "P1 P1 P3", "", "final")
    commits = tuple(GitCommit(str(i), (), float(i), text) for i, text in enumerate(texts))
    messages = CommitMessages(commits)
    tokens = {"P1", "p1", "P12", "XP1", "_P1", "P3", "P1-P3", "refs P3", "!", "É", "İ"}
    tokens.update("".join(chars) for length in range(1, 4)
                  for chars in product("aA_1- é", repeat=length))
    for token in tokens:
        expected = frozenset(commit.sha for commit in commits if re.search(
            r"(?<![a-z0-9_])" + re.escape(token.lower()) + r"(?![a-z0-9_])", commit.message.lower()))
        assert messages.naming(token) == expected, token
    assert messages.naming("P1") is messages.naming("p1")


def test_message_filters_preserve_each_selected_walk_and_old_capture(main):
    git(main, "commit", "--allow-empty", "-m", "P1 first")
    base = git(main, "rev-parse", "HEAD")
    git(main, "checkout", "-b", "side")
    git(main, "commit", "--allow-empty", "-m", "P3 side")
    git(main, "checkout", "main")
    git(main, "commit", "--allow-empty", "-m", "P1 main")
    git(main, "merge", "--no-ff", "side", "-m", "P12 merge")
    history = Checkout(main).read()
    for refs, excluded, first_parent in (
        (("main",), (), False), (("side", "main"), (), False),
        (("main",), (base,), False), (("main",), (), True),
    ):
        walk = tuple(history.commits(refs, *excluded, first_parent=first_parent))
        for token in ("P1", "p3", "P12", "P400"):
            expected = tuple(commit for commit in walk if re.search(
                r"(?<![a-z0-9_])" + token.lower() + r"(?![a-z0-9_])", commit.message.lower()))
            assert tuple(history.commits(refs, *excluded, selected=CommitsNaming(token),
                                        first_parent=first_parent)) == expected
    before = tuple(history.commits(("main",), selected=CommitsNaming("P1")))
    git(main, "commit", "--allow-empty", "-m", "P1 later")
    assert tuple(history.commits(("main",), selected=CommitsNaming("P1"))) == before
    assert len(tuple(Checkout(main).read().commits(("main",), selected=CommitsNaming("P1")))) == len(before) + 1
