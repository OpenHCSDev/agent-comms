import pytest

from agent_comms.projects.checkouts import Checkout
from agent_comms.projects.git_history import CheckoutHistory
from agent_comms.projects.git_errors import GitCommandError
from agent_comms.projects.repository_name import RepositoryName
from source_revision_fixture import SourceRepository


def test_acquired_merge_bases_preserve_git_relations_and_the_ref_cut(tmp_path, monkeypatch):
    source = SourceRepository.create(tmp_path, RepositoryName("example", "merge-bases"))
    main, feature = source.main_commit, source.branch_commit
    tree = source.git(source.checkout, "write-tree").strip()

    def commit(message, *parents):
        return source.git_input(source.checkout, message + "\n", "commit-tree", tree,
                                *(arg for parent in parents for arg in ("-p", parent)))

    unrelated = commit("Separate root")
    left, right = commit("Left", main), commit("Right", main)
    first, second = commit("First merge", left, right), commit("Second merge", right, left)

    def read(*heads):
        result = CheckoutHistory.capture(Checkout(source.checkout))
        result.include(*heads)
        return result

    def forbid_graph(_):
        raise AssertionError("One acquired head pair expanded unrelated retained history")

    with monkeypatch.context() as bounded:
        bounded.setattr(CheckoutHistory, "graph", property(forbid_graph))
        for a, b, expected in (
            (main, main, (main,)), (main, feature, (main,)),
            (feature, main, (main,)), (left, right, (main,)),
            (main, unrelated, ()),
        ):
            assert read(a, b).merge_base(a, b) == expected
        cut = read(main, feature)
        cut.require_read()
        source.git(source.checkout, "update-ref", "refs/heads/main", feature)
        assert cut.merge_base("main", "feature") == (main,)
        missing = "e" * 40
        with pytest.raises(GitCommandError):
            read(main, missing).merge_base(main, missing)

    # Multiple best bases still use the established graph-based winner ordering.
    ambiguous = read(first, second)
    assert set(ambiguous.merge_base(first, second)) == {left, right}
    assert "graph" in vars(ambiguous)
