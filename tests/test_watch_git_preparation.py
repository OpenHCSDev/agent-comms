"""Real Git controls: acquisition is joined; comparison cannot mutate Git or hide absence."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from agent_comms.native_build_inputs import GitLaunch
from agent_comms.phase_evidence import (
    BusStorageAbsent, CommitMerged, EvidenceFound, EvidenceUnread, MainTree, PathsAbsent,
    RetainedPlanReadings,
)
from agent_comms.repo import GitCommandError, Repo, UnavailableCommitError
from agent_comms.todos import DraftPullRequest, UnmergedPullRequest
from agent_comms.work_branches import WorkBranchError, WorkBranches
from source_revision_fixture import SourceRepository


@dataclass(frozen=True)
class GitPreparation:
    source: SourceRepository
    repository: Repo

    @classmethod
    def create(cls, source: SourceRepository, path: Path, *options: str) -> GitPreparation:
        # A transport clone cannot hardlink the remote's unadvertised objects.
        source.git(path.parent, "clone", "--no-local", "--single-branch", "--branch", "main",
                   *options, source.remote_path.as_uri(), str(path))
        return cls(source, Repo(checkout=path, github=source.github))

    def commit_at_remote(self, parent: str, message: str, reference: str) -> str:
        tree = self.source.git(self.source.remote_path, "rev-parse", f"{parent}^{{tree}}").strip()
        commit = self.source.git(self.source.remote_path, "commit-tree", tree, "-p", parent,
                                 "-m", message).strip()
        self.source.git(self.source.remote_path, "update-ref", reference, commit)
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
    with pytest.raises(WorkBranchError, match="Unprepared remote head origin:refs/heads/main"):
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
    Repo.git(repo.checkout, "remote", "add", "publication", str(case.source.remote_path))
    Repo.git(repo.checkout, "update-ref", "refs/remotes/publication/main", base)
    Repo.git(repo.checkout, "config", "branch.main.remote", "publication")
    Repo.git(repo.checkout, "config", "branch.main.merge", "refs/heads/main")
    if upstream == "deleted":
        case.source.git(case.source.remote_path, "update-ref", "-d", "refs/heads/main")
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
    (unpublished,) = difference.unpublished
    assert (unpublished.head, unpublished.commits) == (head, 1)
    assert unpublished.upstream == Repo.git(
        repo.checkout, "for-each-ref", "--format=%(upstream)", "refs/heads/main",
    ).strip()
    assert case.snapshot() == prepared


def test_upstream_preparation_batches_only_required_heads_per_remote(
    git_preparation: GitPreparation, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = git_preparation
    repo = case.repository
    base = case.source.main_commit
    Repo.git(repo.checkout, "config", "--replace-all", "remote.origin.fetch",
             "+refs/heads/*:refs/remotes/origin/*")
    Repo.git(repo.checkout, "remote", "add", "publication", case.source.remote_path.as_uri())
    # Distinct upstream refs share one actual object; neither refs nor heads may cause extra calls.
    for remote in ("origin", "publication"):
        for index in range(2):
            reference = f"refs/heads/published-{index}"
            case.source.git(case.source.remote_path, "update-ref", reference, base)
            branch = f"{remote}-{index}"
            Repo.git(repo.checkout, "branch", branch, base)
            Repo.git(repo.checkout, "update-ref", f"refs/remotes/{remote}/published-{index}", base)
            Repo.git(repo.checkout, "config", f"branch.{branch}.remote", remote)
            Repo.git(repo.checkout, "config", f"branch.{branch}.merge", reference)
    unrelated = case.commit_at_remote(base, "Unrequested advertisement", "refs/heads/unrelated")
    assert not repo.has_commit(unrelated)
    before = case.snapshot()
    trace = tmp_path / "preparation.trace2.jsonl"
    reader = WorkBranches(repo.checkout)
    with monkeypatch.context() as environment:
        environment.setenv("GIT_TRACE2_EVENT", str(trace))
        reader.prepare_upstreams()
    events = tuple(json.loads(line) for line in trace.read_text().splitlines())
    commands = tuple(event["argv"] for event in events if event.get("event") == "start")
    listings = sorted(command[command.index("ls-remote"):] for command in commands
                      if "ls-remote" in command)
    assert listings == [["ls-remote", "--refs", "origin"],
                        ["ls-remote", "--refs", "publication"]]
    batches = tuple(command for command in commands if "cat-file" in command)
    assert len(batches) == 2
    assert all("--batch-check=%(objectname) %(objecttype)" in command for command in batches)
    assert not any("fetch" in command for command in commands)
    assert not repo.has_commit(unrelated)
    assert reader.selected_heads("origin", "refs/heads/unrelated") == (unrelated,)
    assert reader.selected_heads("publication", "refs/heads/unrelated") == (unrelated,)
    assert case.snapshot() == before

    # A prepared PR reuses the same remote cut and requires only its actual base and head.
    observation = tmp_path / "comparison.trace2.jsonl"
    ready = UnmergedPullRequest(99, "ready", base)
    with monkeypatch.context() as environment:
        environment.setenv("GIT_TRACE2_EVENT", str(observation))
        ready.prepare_comparison(reader)
        ready.require_comparison(reader)
        (empty,) = ready.empty_changes(reader)
        assert (empty.base, empty.head, empty.paths) == (base, base, ())
        reader.require_upstream("origin-0")
    compared = tuple(json.loads(line) for line in observation.read_text().splitlines())
    assert not any(event.get("event") == "start" and
                   any(verb in event.get("argv", ()) for verb in ("ls-remote", "fetch"))
                   for event in compared)
    assert not repo.has_commit(unrelated)
    assert case.snapshot() == before

    newer = case.commit_at_remote(base, "Next publication cut", "refs/heads/published-0")
    assert reader.remote_heads("origin", "refs/heads/published-0") == (base,)
    fresh = WorkBranches(repo.checkout)
    assert fresh.remote_heads("origin", "refs/heads/published-0") == (newer,)
    assert fresh.selected_heads("origin", "refs/heads/deleted") == ()
    with pytest.raises(UnavailableCommitError, match=newer):
        fresh.require_upstream("origin-0")
    assert not repo.has_commit(newer)
    assert case.snapshot() == before


def test_missing_objects_are_not_negative_ancestry_and_local_siblings_are(git_preparation):
    case = git_preparation
    repo = case.repository
    remote = case.commit_at_remote(case.source.main_commit, "Remote sibling", "refs/heads/main")
    SourceRepository.git(repo.checkout, "commit", "--allow-empty", "-m", "Local sibling")
    local = Repo.git(repo.checkout, "rev-parse", "HEAD").strip()
    before = case.snapshot()
    with pytest.raises(GitCommandError, match=remote):
        repo.contains(remote, local)
    assert case.snapshot() == before
    Repo.require_commits(repo.checkout, "origin", remote)
    prepared = case.snapshot()
    assert not repo.contains(remote, local)
    assert not repo.contains(local, remote)
    assert repo.contains(remote[:12], remote)
    with pytest.raises(UnavailableCommitError, match="required Git objects unavailable"):
        repo.contains(local, "not-a-revision")
    assert case.snapshot() == prepared


def test_failed_plan_acquisition_is_unknown_and_retries_without_main_moving(git_preparation):
    case = git_preparation
    repo = case.repository
    head = case.commit_at_remote(case.source.main_commit, "Future plan", "refs/pull/99/head")
    evidence = CommitMerged(head)
    plans = RetainedPlanReadings()
    Repo.git(repo.checkout, "remote", "set-url", "origin", str(repo.checkout / "absent-remote"))
    failed = plans.prepare_reading(repo, (), (evidence,))
    assert isinstance(failed.read(evidence), EvidenceUnread)
    assert head in failed.describe(evidence) and "origin" in failed.describe(evidence)
    plans.observe_reading(failed, (evidence,))
    Repo.git(repo.checkout, "remote", "set-url", "origin", str(case.source.remote_path))
    recovered = plans.prepare_reading(repo, (), (evidence,))
    assert recovered.main.commit == failed.main.commit
    assert repo.has_commit(head)
    prepared = case.snapshot()
    plans.observe_reading(recovered, (evidence,))
    assert recovered.read(evidence) == EvidenceFound(False)
    assert case.snapshot() == prepared


def test_unexpected_plan_object_loss_is_unknown_not_phase_completion(git_preparation):
    repo = git_preparation.repository
    reading = RetainedPlanReadings().prepare_reading(repo, (), ())
    missing = CommitMerged("f" * 40)
    assert isinstance(reading.read(missing), EvidenceUnread)
    assert not reading.holds(missing)


def test_promised_content_is_unreadable_not_absent_and_never_lazily_acquired(
    source_repository: SourceRepository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = source_repository
    source.git(source.checkout, "switch", "feature")
    directory = source.checkout / "src"
    directory.mkdir()
    (directory / "promised.py").write_text("value = 1\n")
    (directory / "deleted.py").write_text("deleted = 1\n")
    base = source.publish_change("Before content changes", "src")
    (directory / "promised.py").write_text("value = 1\nother = 2\n")
    (directory / "added.py").write_text("added = 1\n")
    (directory / "deleted.py").unlink()
    head = source.publish_change("Content changes", "src")
    source.git(source.checkout, "push", "origin", "HEAD:refs/heads/main")
    source.git(source.remote_path, "config", "uploadpack.allowFilter", "true")
    source.git(source.remote_path, "config", "uploadpack.allowAnySHA1InWant", "true")
    case = GitPreparation.create(source, tmp_path / "promisor", "--no-checkout", "--filter=blob:none")
    repo = case.repository
    paths = ("src/added.py", "src/deleted.py", "src/promised.py")
    objects = tuple(dict.fromkeys(
        blob for commit in (base, head)
        for blob in Repo.tree_objects(source.checkout, commit, paths).values()
    ))
    # The fixture probe forbids demand acquisition explicitly, before any content read.
    for blob in objects:
        assert GitLaunch._run(repo.checkout, "--no-lazy-fetch", "cat-file", "-e", blob).returncode != 0
    common = Path(Repo.git(repo.checkout, "rev-parse", "--path-format=absolute", "--git-common-dir").strip())
    assert tuple((common / "objects/pack").glob("*.promisor"))
    assert Repo.git(repo.checkout, "config", "--bool", "remote.origin.promisor").strip() == "true"
    trace = tmp_path / "observation.trace2.jsonl"
    monkeypatch.setenv("GIT_TRACE2_EVENT", str(trace))
    before = case.snapshot()
    main = MainTree(repo, head)
    assert main.has("src/promised.py")
    assert not main.has("src/deleted.py")
    assert main.missing(paths) == (False, True, False)
    with pytest.raises(UnavailableCommitError):
        main.module("src/promised.py")
    with pytest.raises(UnavailableCommitError):
        tuple(main.modules(("src",)))
    reader = WorkBranches(repo.checkout)
    changed = reader.changed(base, head)
    assert changed == frozenset(paths)
    with pytest.raises(UnavailableCommitError):
        reader.source_growth(base, head, changed)
    with pytest.raises(GitCommandError):
        Repo.git_pipeline(repo.checkout, ("cat-file", "blob", objects[0]), ("hash-object", "--stdin"))
    evidence = BusStorageAbsent()
    plans = RetainedPlanReadings()
    failed = plans.prepare_reading(repo, (), (evidence,))
    plans.observe_reading(failed, (evidence,))
    assert isinstance(failed.read(evidence), EvidenceUnread)
    assert "required blob unavailable" in failed.describe(evidence)
    assert not failed.holds(PathsAbsent(("src/promised.py",)))
    assert case.snapshot() == before
    events = tuple(json.loads(line) for line in trace.read_text().splitlines())
    assert not any(event.get("event") == "child_start" and
                   any("upload-pack" in argument for argument in event.get("argv", ()))
                   for event in events)
    monkeypatch.delenv("GIT_TRACE2_EVENT")
    # Only this explicit joined acquisition changes Git. The same-main unknown must retry.
    Repo.git(repo.checkout, "fetch", "--quiet", "--no-tags", "origin", *objects)
    prepared = case.snapshot()
    recovered = plans.prepare_reading(repo, (), (evidence,))
    plans.observe_reading(recovered, (evidence,))
    assert recovered.main.commit == failed.main.commit == head
    assert recovered.read(evidence) == EvidenceFound(True)
    assert dict(reader.source_growth(base, head, changed).files) == {
        "src/added.py": 1, "src/deleted.py": -1, "src/promised.py": 1,
    }
    assert case.snapshot() == prepared


def test_pushed_conflict_markers_are_the_branchs_fact_not_an_error(tmp_path):
    """Foreign branch text that does not parse is reported with its path and line, and
    refuses acceptance; it never escapes the heartbeat's growth count."""
    checkout = tmp_path / "repo"
    checkout.mkdir()
    for argument in (("init", "-q", "-b", "main"), ("config", "user.email", "t@example.com"),
                     ("config", "user.name", "t")):
        Repo.git(checkout, *argument)
    (checkout / "src").mkdir()
    module = checkout / "src" / "owner.py"
    module.write_text("value = 1\n")
    Repo.git(checkout, "add", ".")
    Repo.git(checkout, "commit", "-q", "-m", "base")
    base = Repo.git(checkout, "rev-parse", "HEAD").strip()
    module.write_text("<<<<<<< HEAD\nvalue = 1\n=======\nvalue = 2\n>>>>>>> other\n")
    Repo.git(checkout, "commit", "-q", "-am", "conflict markers pushed")
    head = Repo.git(checkout, "rev-parse", "HEAD").strip()
    reader = WorkBranches(checkout)
    growth = reader.source_growth(base, head, reader.changed(base, head))
    assert [(source.path, source.line) for source in growth.unparsed] == [("src/owner.py", 1)]
    with pytest.raises(WorkBranchError, match="src/owner.py:1"):
        growth.require_parsed()
