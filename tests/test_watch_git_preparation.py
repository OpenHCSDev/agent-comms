"""Real Git controls: acquisition is joined; comparison cannot mutate Git or hide absence."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import pytest

from agent_comms.phase_evidence import CommitMerged, EvidenceFound, EvidenceUnread, RetainedPlanReadings
from agent_comms.repo import Repo, UnavailableCommitError
from agent_comms.todos import DraftPullRequest, UnmergedPullRequest
from agent_comms.work_branches import WorkBranchError, WorkBranches
from source_revision_fixture import SourceRepository


@dataclass(frozen=True)
class GitPreparation:
    source: SourceRepository
    repository: Repo

    @classmethod
    def create(cls, source: SourceRepository, path: Path) -> GitPreparation:
        # A transport clone cannot hardlink the remote's unadvertised objects.
        source.git(path.parent, "clone", "--no-local", "--single-branch", "--branch", "main",
                   str(source.remote), str(path))
        return cls(source, Repo(checkout=path, github=source.github))

    def commit_at_remote(self, parent: str, message: str, reference: str) -> str:
        tree = self.source.git(self.source.remote, "rev-parse", f"{parent}^{{tree}}").strip()
        commit = self.source.git(self.source.remote, "commit-tree", tree, "-p", parent,
                                 "-m", message).strip()
        self.source.git(self.source.remote, "update-ref", reference, commit)
        return commit

    def snapshot(self) -> tuple[tuple[str, str], ...]:
        common = Path(Repo.git(self.repository.checkout, "rev-parse", "--path-format=absolute",
                               "--git-common-dir").strip())
        return tuple(sorted(
            (str(path.relative_to(common)), hashlib.sha256(path.read_bytes()).hexdigest())
            for root in (common / "objects", common / "refs", common / "logs")
            for path in root.rglob("*") if path.is_file()
        )) + tuple((name, hashlib.sha256((common / name).read_bytes()).hexdigest())
                   for name in ("FETCH_HEAD", "packed-refs") if (common / name).is_file())


@pytest.fixture
def git_preparation(source_repository: SourceRepository, tmp_path: Path) -> GitPreparation:
    return GitPreparation.create(source_repository, tmp_path / "observer")


def test_missing_ready_pr_objects_are_acquired_once_and_comparison_is_local(git_preparation):
    case = git_preparation
    repo = case.repository
    base = case.commit_at_remote(case.source.main_commit, "New base", "refs/heads/main")
    head = case.commit_at_remote(base, "Empty ready head", "refs/pull/99/head")
    assert not repo.has_commit(base) and not repo.has_commit(head)
    reader = WorkBranches(repo.checkout)
    ready = UnmergedPullRequest(99, "ready", head)
    draft = DraftPullRequest(99, "draft", head)
    before = case.snapshot()
    draft.prepare_comparison(reader)
    assert draft.empty_changes(reader) == ()
    assert case.snapshot() == before
    with pytest.raises(WorkBranchError, match="was not prepared"):
        ready.empty_changes(reader)
    ready.prepare_comparison(reader)
    assert repo.has_commit(base) and repo.has_commit(head)
    prepared = case.snapshot()
    (empty,) = ready.empty_changes(reader)
    assert (empty.base, empty.head, empty.paths) == (base, head, ())
    assert reader.pull_request_base("main", head) == base
    reader.pull_request_patch_id("main", head)
    # A later published base cannot change this pass's selected base or acquire another object.
    newer = case.commit_at_remote(head, "Later base", "refs/heads/main")
    assert reader.pull_request_base("main", head) == base
    assert not repo.has_commit(newer)
    assert case.snapshot() == prepared


@pytest.mark.parametrize("upstream", ("custom", "deleted", "local", "none"))
def test_upstream_preparation_preserves_publication_and_fallback_counts(git_preparation, upstream):
    case = git_preparation
    repo = case.repository
    base = case.source.main_commit
    SourceRepository.git(repo.checkout, "commit", "--allow-empty", "-m", "Unpublished work")
    head = Repo.git(repo.checkout, "rev-parse", "HEAD").strip()
    Repo.git(repo.checkout, "remote", "add", "publication", str(case.source.remote))
    Repo.git(repo.checkout, "update-ref", "refs/remotes/publication/main", base)
    Repo.git(repo.checkout, "config", "branch.main.remote", "publication")
    Repo.git(repo.checkout, "config", "branch.main.merge", "refs/heads/main")
    if upstream == "deleted":
        case.source.git(case.source.remote, "update-ref", "-d", "refs/heads/main")
    elif upstream == "local":
        Repo.git(repo.checkout, "branch", "local-base", base)
        Repo.git(repo.checkout, "config", "branch.main.remote", ".")
        Repo.git(repo.checkout, "config", "branch.main.merge", "refs/heads/local-base")
    elif upstream == "none":
        Repo.git(repo.checkout, "config", "--remove-section", "branch.main")
    else:
        published = case.commit_at_remote(base, "Published sibling", "refs/heads/main")
        assert not repo.has_commit(published)
    reader = WorkBranches(repo.checkout)
    reader.prepare_upstreams()
    difference = reader.difference(reader.origin_main)
    prepared = case.snapshot()
    (unpublished,) = reader.unpushed(difference)
    assert (unpublished.head, unpublished.commits) == (head, 1)
    assert unpublished.upstream == reader.upstreams["main"].tracking
    assert case.snapshot() == prepared


def test_missing_objects_are_not_negative_ancestry_and_local_siblings_are(git_preparation):
    case = git_preparation
    repo = case.repository
    remote = case.commit_at_remote(case.source.main_commit, "Remote sibling", "refs/heads/main")
    SourceRepository.git(repo.checkout, "commit", "--allow-empty", "-m", "Local sibling")
    local = Repo.git(repo.checkout, "rev-parse", "HEAD").strip()
    before = case.snapshot()
    with pytest.raises(UnavailableCommitError, match=remote):
        repo.contains(remote, local)
    assert case.snapshot() == before
    Repo.require_commits(repo.checkout, "origin", remote)
    prepared = case.snapshot()
    assert not repo.contains(remote, local)
    assert not repo.contains(local, remote)
    assert repo.contains(remote[:12], remote)
    with pytest.raises(UnavailableCommitError, match="required commit objects unavailable"):
        repo.contains(local, "not-a-revision")
    assert case.snapshot() == prepared


def test_failed_plan_acquisition_is_unknown_and_retries_without_main_moving(git_preparation):
    case = git_preparation
    repo = case.repository
    head = case.commit_at_remote(case.source.main_commit, "Future plan", "refs/pull/99/head")
    evidence = CommitMerged(head)
    plans = RetainedPlanReadings()
    Repo.git(repo.checkout, "remote", "set-url", "origin", str(repo.checkout / "absent-remote"))
    failed = plans.prepare(repo, (), (evidence,))
    assert isinstance(failed.read(evidence), EvidenceUnread)
    assert head in failed.describe(evidence) and "origin" in failed.describe(evidence)
    plans.observe(failed, (evidence,))
    Repo.git(repo.checkout, "remote", "set-url", "origin", str(case.source.remote))
    recovered = plans.prepare(repo, (), (evidence,))
    assert recovered.main.commit == failed.main.commit
    assert repo.has_commit(head)
    prepared = case.snapshot()
    plans.observe(recovered, (evidence,))
    assert recovered.read(evidence) == EvidenceFound(False)
    assert case.snapshot() == prepared


def test_unexpected_plan_object_loss_is_unknown_not_phase_completion(git_preparation):
    repo = git_preparation.repository
    reading = RetainedPlanReadings().prepare(repo, (), ())
    missing = CommitMerged("f" * 40)
    assert isinstance(reading.read(missing), EvidenceUnread)
    assert not reading.holds(missing)
