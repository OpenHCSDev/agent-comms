"""One pass's repository cut against worktrees that appear or vanish while it reads."""
import re
import pickle
from itertools import product
from operator import setitem
from pathlib import Path

import pytest

import fixture_git
from agent_comms.projects.checkouts import (
    Checkout,
    CheckoutHistory,
    CommitMessages,
    CommitsNaming,
    GitCommit,
    GitGraphRevision,
    GitHistoryRevision,
    RepositoryReads,
)
from agent_comms.projects.git_errors import FailedGitCommand
from agent_comms.projects.repo import Repo
from agent_comms.projects.repository_name import RepositoryName
from agent_comms.planning.orders import ProgressRead

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


def test_retention_keeps_an_unconsumed_repository_cut_and_its_worktree_projection(
    main: Path,
) -> None:
    reads = ProgressRead()
    repository = Repo(
        checkout=main,
        github=RepositoryName('example', 'retained-cut'),
    )
    history = reads.history_for(Checkout(main))
    worktrees = reads.worktrees(repository)
    assert CheckoutHistory.graph.attrname not in history.__dict__

    retained = ProgressRead.retaining(reads)

    assert retained.history_for(Checkout(main)) is history
    assert retained.worktrees(repository) is worktrees


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


def test_retaining_reloads_a_new_linked_worktree_replacement_setting(main: Path) -> None:
    git(main, 'config', 'extensions.worktreeConfig', 'true')
    git(main, 'commit', '--allow-empty', '-m', 'P303 linked original')
    original = git(main, 'rev-parse', 'HEAD')
    replacement = git(main, 'commit-tree', 'HEAD^{tree}', '-m', 'P304 linked replacement')
    git(main, 'replace', original, replacement)
    linked = main.parent / 'linked'
    git(main, 'worktree', 'add', '-b', 'linked', str(linked))
    reads = RepositoryReads()
    previous = reads.history_for(Checkout(linked))
    previous.graph
    assert previous.messages.naming('P304') == {original}

    worktree_config = Path(git(
        linked, 'rev-parse', '--path-format=absolute', '--git-path', 'config.worktree',
    ))
    assert worktree_config in dict(previous.source_revision.sources)
    assert not worktree_config.exists()
    git(linked, 'config', '--worktree', 'core.useReplaceRefs', 'false')

    following = RepositoryReads.retaining(reads).history_for(Checkout(linked))
    assert following.policy.replacement_setting == 'false'
    assert following.messages.naming('P303') == {original}


def test_equal_graph_source_adopts_its_fresh_file_revision(main: Path, monkeypatch) -> None:
    reads = RepositoryReads()
    previous = reads.history_for(Checkout(main))
    head = git(main, 'rev-parse', 'HEAD')
    previous.include(head)
    previous.graph
    (Checkout(main).common_dir / 'config').touch()

    retained = RepositoryReads.retaining(reads)
    following = retained.history_for(Checkout(main))
    assert following is previous
    assert following.sources_current()
    assert following.merge_base(head, head) == (head,)

    monkeypatch.setattr(
        CheckoutHistory,
        'capture',
        classmethod(lambda cls, checkout: pytest.fail('current retained source recaptured')),
    )
    assert RepositoryReads.retaining(retained).history_for(Checkout(main)) is following


def test_moved_worktree_head_does_not_rediscover_unchanged_graph_sources(
    main: Path, monkeypatch,
) -> None:
    reads = RepositoryReads()
    previous = reads.history_for(Checkout(main))
    git(main, 'commit', '--allow-empty', '-m', 'move only the branch and worktree head')
    moved = git(main, 'rev-parse', 'HEAD')

    monkeypatch.setattr(
        GitGraphRevision,
        'capture',
        classmethod(lambda cls, checkout: pytest.fail('unchanged graph sources rediscovered')),
    )
    following = RepositoryReads.retaining(reads).history_for(Checkout(main))
    assert following.head_for(Checkout(main)).revision() == moved
    assert following.source_revision.graph is previous.source_revision.graph


@pytest.mark.parametrize('read', ('graph', 'merge-base'))
def test_lazy_git_query_refuses_a_moved_captured_source(main: Path, read: str) -> None:
    git(main, 'commit', '--allow-empty', '-m', 'captured original')
    original = git(main, 'rev-parse', 'HEAD')
    history = Checkout(main).read()
    history.include(original)
    replacement = git(main, 'commit-tree', 'HEAD^{tree}', '-m', 'late replacement')
    git(main, 'replace', original, replacement)

    with pytest.raises(OSError, match='changed after their captured cut'):
        if read == 'graph':
            history.graph
        else:
            history.merge_base(original, original)


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


def test_head_paths_skip_worktree_submodules_and_logs(main: Path) -> None:
    """Linked worktree directories hold submodule trees (modules/) and reflogs (logs/) that
    git worktree list never reads; walking them recursively retained 30k+ FileRevision objects
    on the shared worker host."""
    linked = main.parent / "linked-with-modules"
    git(main, "worktree", "add", "-b", "linked-mod", str(linked))
    admin = Checkout(main).common_dir / "worktrees" / "linked-with-modules"
    submodule_file = admin / "modules" / "sub" / "objects" / "dummy"
    submodule_file.parent.mkdir(parents=True)
    submodule_file.write_text("x")
    log_file = admin / "logs" / "HEAD"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text("log")

    paths = set(GitHistoryRevision.head_paths(Checkout(main)))
    assert admin in paths
    assert admin / "HEAD" in paths
    assert admin / "gitdir" in paths
    assert submodule_file not in paths
    assert log_file not in paths
    assert not any("modules" in path.parts or "logs" in path.parts for path in paths)


def test_remote_cuts_do_not_capture_history_before_fetch(main: Path, monkeypatch) -> None:
    """ClosedPullRequestWatch and prepare_pull_request call remote_heads before require_commits.
    If remote_cuts captures CheckoutHistory prematurely, git fetch rewriting packed-refs trips
    GitGraphSource.require_current() with 'Git history sources changed after their captured cut'."""
    reads = RepositoryReads()
    linked = main.parent / "linked-remote-cuts"
    git(main, "worktree", "add", "-b", "linked-remote", str(linked))

    with monkeypatch.context() as patched:
        patched.setattr(
            CheckoutHistory,
            "capture",
            classmethod(lambda cls, checkout, previous=None: pytest.fail(
                "remote_cuts captured CheckoutHistory before history_for")),
        )
        main_cut = reads.remote_cuts(Checkout(main))
        assert reads.remote_cuts(Checkout(linked)) is main_cut

    # Simulate git fetch / pack-refs mutating packed-refs after remote_cuts and before history_for.
    git(main, "pack-refs", "--all")
    head = git(main, "rev-parse", "HEAD")
    history = reads.history_for(Checkout(main))
    history.include(head)
    assert head in history.graph

