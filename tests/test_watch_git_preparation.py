"""Real Git controls: acquisition is joined; comparison cannot mutate Git or hide absence."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import threading
from concurrent.futures import Future
from collections.abc import Callable
from contextlib import AbstractContextManager, ExitStack, contextmanager, nullcontext
from dataclasses import dataclass
from functools import partial
from pathlib import Path

import pytest

from agent_comms.child_process import (
    AsyncWork, ChildRetirementFailed, GitChildPermission, GitChildren, ObservedProcess,
    OwnedGitOpening, ReadWork, SpawnedProcess, SynchronousProcess, join_retirement,
)
from agent_comms.native_build_inputs import (
    ConfiguredChildStdio, DetachedChildStdio, GitAcquisitionCancelled, GitLaunch,
)
from agent_comms.coordinator import Coordination
from agent_comms.comms import Comms
from agent_comms.message_bus import Watch
from agent_comms.service_passes import ServicePassStore
from git_shutdown_fixture import HeldGitHTTP
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


@pytest.mark.parametrize("method", ("GET", "POST"))
async def test_shutdown_cancels_publication_http_and_prevents_next_remote(
    git_preparation: GitPreparation, tmp_path: Path, method: str,
) -> None:
    case = git_preparation
    repo = case.repository
    missing = case.commit_at_remote(case.source.main_commit, "New remote head", "refs/heads/main")
    assert not repo.has_commit(missing)
    children = GitChildren()
    reader = WorkBranches(repo.checkout)
    published = tmp_path / "published"
    with HeldGitHTTP.serving(case.source.remote_path, method) as remote:
        Repo.git(repo.checkout, "config", "protocol.version", "0")
        Repo.git(repo.checkout, "remote", "set-url", "origin", remote.url)
        Repo.git(repo.checkout, "remote", "add", "second", remote.url + "-never")
        Repo.git(repo.checkout, "branch", "z-second", case.source.main_commit)
        Repo.git(repo.checkout, "update-ref", "refs/remotes/second/main", case.source.main_commit)
        Repo.git(repo.checkout, "config", "branch.z-second.remote", "second")
        Repo.git(repo.checkout, "config", "branch.z-second.merge", "refs/heads/main")

        def prepare() -> None:
            reader.prepare_upstreams()
            published.touch()

        with OwnedGitOpening(children).bound():
            pending = asyncio.create_task(Coordination.run_worker(prepare))
        try:
            assert await asyncio.to_thread(remote.entered.wait, 10)
            with children.lock:
                handles = tuple(children.active)
                members = tuple(member for child in handles
                                for member in child.platform.group_members(child.identity))
            assert handles and len(members) > len(handles), "Actual Git HTTP helper was not captured"
            shutdown = children.start_shutdown()
            assert children.start_shutdown() is shutdown
            await join_retirement(shutdown)
            with pytest.raises(GitAcquisitionCancelled):
                await pending
            assert not published.exists()
            assert not any(path.endswith("-never/info/refs") for _method, path in remote.requests)
            assert all(child.retired for child in handles)
            assert all(not member.alive() for member in members)
            assert not children.active
            if method == "GET":
                assert "origin" not in reader._remote_heads
            with OwnedGitOpening(children).bound(), pytest.raises(GitAcquisitionCancelled):
                reader.prepare_upstreams()
        finally:
            remote.release.set()
            await join_retirement(children.start_shutdown())
            await asyncio.gather(pending, return_exceptions=True)


async def test_shutdown_retires_both_pipeline_children_and_readwork_child(
    git_preparation: GitPreparation,
) -> None:
    case = git_preparation
    repo = case.repository
    with HeldGitHTTP.serving(case.source.remote_path, "GET") as remote:
        Repo.git(repo.checkout, "config", "protocol.version", "0")
        Repo.git(repo.checkout, "remote", "set-url", "origin", remote.url)
        for work in (AsyncWork, ReadWork):
            children = GitChildren()
            remote.entered.clear()

            def pipeline() -> bytes:
                with Repo.acquiring():
                    return Repo._pipe(repo.checkout, ("ls-remote", "--refs", "origin"),
                                      ("hash-object", "--stdin"), timeout=10).stdout

            with OwnedGitOpening(children).bound():
                pending = asyncio.create_task(work.run_async(pipeline))
            try:
                assert await asyncio.to_thread(remote.entered.wait, 10)
                async with asyncio.timeout(10):
                    while True:
                        with children.lock:
                            handles = tuple(children.active)
                        if len(handles) == 2:
                            break
                        await asyncio.sleep(.01)
                await join_retirement(children.start_shutdown())
                with pytest.raises(GitAcquisitionCancelled):
                    await pending
                assert all(child.retired for child in handles)
                assert not children.active
            finally:
                await join_retirement(children.start_shutdown())
                await asyncio.gather(pending, return_exceptions=True)


class PausedRegistration(GitChildren):
    def __init__(self) -> None:
        super().__init__()
        self.gated: Future[SpawnedProcess] = Future()
        self.release = threading.Event()

    def pause(self, child: SpawnedProcess) -> None:
        self.gated.set_result(child)
        if not self.release.wait(10):
            raise TimeoutError("Gated launch control was never released")


class BeforeRegistration(PausedRegistration):
    def register(self, child: SpawnedProcess, permission: GitChildPermission) -> None:
        self.pause(child)
        super().register(child, permission)


class AfterRegistration(PausedRegistration):
    def register(self, child: SpawnedProcess, permission: GitChildPermission) -> None:
        super().register(child, permission)
        self.pause(child)


@pytest.mark.parametrize("collection", (BeforeRegistration, AfterRegistration))
async def test_shutdown_closes_gated_launch_before_user_code(
    collection: type[PausedRegistration], tmp_path: Path,
) -> None:
    children = collection()
    executed = tmp_path / "executed"

    def exchange() -> None:
        with Repo.acquiring(), Repo.opening(
            (sys.executable, "-c", "from pathlib import Path; import sys; Path(sys.argv[1]).touch()",
             str(executed)), env=os.environ, stdio=DetachedChildStdio(
                 stdout=subprocess.PIPE, stderr=subprocess.PIPE),
        ) as process:
            process.communicate(timeout=10)

    with OwnedGitOpening(children).bound():
        pending = asyncio.create_task(AsyncWork.run_async(exchange))
    try:
        child = await asyncio.wait_for(asyncio.wrap_future(children.gated), 10)
        assert child.alive() and not executed.exists()
        await join_retirement(children.start_shutdown())
        children.release.set()
        with pytest.raises(GitAcquisitionCancelled):
            await pending
        assert child.retired and not executed.exists()
        assert not children.active
    finally:
        children.release.set()
        await join_retirement(children.start_shutdown())
        await asyncio.gather(pending, return_exceptions=True)


async def test_shutdown_protects_entered_commit_push_worktree_and_later_child(
    git_preparation: GitPreparation, tmp_path: Path,
) -> None:
    repo = git_preparation.repository
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    entered = tmp_path / "commit-entered"
    release = tmp_path / "commit-release"
    hook = hooks / "pre-commit"
    hook.write_text(f"#!{sys.executable}\nfrom pathlib import Path\nimport time\n"
                    f"Path({str(entered)!r}).touch()\n"
                    f"while not Path({str(release)!r}).exists(): time.sleep(.01)\n")
    hook.chmod(0o755)
    Repo.git(repo.checkout, "config", "core.hooksPath", str(hooks))
    children = GitChildren()
    worktree = tmp_path / "protected-worktree"

    def business() -> str:
        SourceRepository.git(repo.checkout, "commit", "--allow-empty", "-m", "Protected commit")
        Repo.git(repo.checkout, "push", "origin", "HEAD:refs/heads/protected")
        Repo.git(repo.checkout, "worktree", "add", "--detach", str(worktree), "HEAD")
        head = Repo.git(worktree, "rev-parse", "HEAD").strip()
        Repo.git(repo.checkout, "worktree", "remove", str(worktree))
        return head

    with OwnedGitOpening(children).bound():
        pending = asyncio.create_task(AsyncWork.run_async(business))
    try:
        async with asyncio.timeout(10):
            while not entered.exists():
                assert not pending.done()
                await asyncio.sleep(.01)
        with children.lock:
            handles = tuple(children.active)
        assert handles
        await join_retirement(children.start_shutdown())
        assert all(child.alive() for child in handles)
        assert not pending.done(), "Shutdown abandoned the entered commit"
        release.touch()
        head = await pending
        assert Repo.git(git_preparation.source.remote_path, "rev-parse", "refs/heads/protected").strip() == head
        assert not worktree.exists() and not children.active
    finally:
        release.touch()
        await join_retirement(children.start_shutdown())
        await asyncio.gather(pending, return_exceptions=True)


async def test_shutdown_retires_exited_leaders_pipe_holder(tmp_path: Path) -> None:
    children = GitChildren()
    leader: Future[SpawnedProcess] = Future()
    holding = threading.Event()

    def exchange() -> None:
        with Repo.acquiring(), Repo.opening(
            (sys.executable, "-c", "from git_shutdown_fixture import exited_leader; exited_leader()"),
            env=dict(os.environ, PYTHONPATH=os.pathsep.join((str(Path(__file__).parent),
                                                          os.environ.get("PYTHONPATH", "")))),
            stdio=DetachedChildStdio(stdout=subprocess.PIPE, stderr=subprocess.PIPE),
        ) as process:
            assert process.stdout is not None
            assert process.stdout.readline() == b"pipe holder ready\n"
            assert process.wait(timeout=10) == 0
            with children.lock:
                (child,) = children.active
            leader.set_result(child)
            holding.set()
            process.communicate(timeout=10)
        (tmp_path / "published").touch()

    with OwnedGitOpening(children).bound():
        pending = asyncio.create_task(AsyncWork.run_async(exchange))
    try:
        assert await asyncio.to_thread(holding.wait, 10)
        child = leader.result(timeout=0)
        members = child.platform.group_members(child.identity)
        assert not child.alive() and members
        await join_retirement(children.start_shutdown())
        with pytest.raises(GitAcquisitionCancelled):
            await pending
        assert child.retired and all(not member.alive() for member in members)
        assert not children.active and not (tmp_path / "published").exists()
    finally:
        await join_retirement(children.start_shutdown())
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("owned", (False, True), ids=("bootstrap", "core"))
def test_git_opening_preserves_inherited_stdin(owned: bool) -> None:
    payload = b"inherited descriptor, not DEVNULL\n"
    read_fd, write_fd = os.pipe()
    with os.fdopen(read_fd, "rb") as given:
        with os.fdopen(write_fd, "wb") as producer:
            producer.write(payload)
        with SpawnedProcess.launch(
            (sys.executable, "-c", "import sys; from git_shutdown_fixture import inherited_stdio; "
             "inherited_stdio(sys.argv[1] == 'core')", "core" if owned else "bootstrap"),
            env=dict(os.environ, PYTHONPATH=os.pathsep.join((str(Path(__file__).parent),
                                                          os.environ.get("PYTHONPATH", "")))),
            stdio=ConfiguredChildStdio(
                stdin=given, stdout=subprocess.PIPE, stderr=subprocess.PIPE),
        ) as child:
            result = child.collect(timeout=10)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert result.stdout == payload


def test_git_bootstrap_opening_works_without_core_imports(git_preparation: GitPreparation) -> None:
    source = Path(__file__).resolve().parents[1] / "src/agent_comms/native_build_inputs.py"
    program = (
        "import runpy,sys; module=runpy.run_path(sys.argv[1]); "
        "r=module['GitLaunch']._run(sys.argv[2], 'rev-parse', 'HEAD'); "
        "assert r.returncode == 0; "
        "assert not any(n == 'agent_comms' or n.startswith('agent_comms.') for n in sys.modules); "
        "sys.stdout.buffer.write(r.stdout)"
    )
    with SpawnedProcess.launch(
        (sys.executable, "-I", "-c", program, str(source), str(git_preparation.repository.checkout)),
        stdio=DetachedChildStdio(stdout=subprocess.PIPE, stderr=subprocess.PIPE),
    ) as child:
        result = child.collect(timeout=10)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert result.stdout.decode().strip() == git_preparation.source.main_commit


async def test_cancelled_watch_preserves_existing_service_failure(
    comms: Comms, tmp_path: Path,
) -> None:
    failed = tmp_path / "failed-callback"

    class CancelledWatch(Watch, declared_name=None):
        async def run(self, opened_wire: Callable[[], AbstractContextManager[Comms]]) -> None:
            raise GitAcquisitionCancelled("Private acquisition closed")

        def failed(self, opened_wire: Callable[[], AbstractContextManager[Comms]],
                   error: Exception) -> None:
            failed.touch()

    watch = CancelledWatch()
    passes = ServicePassStore.at(comms.root)
    passes.failed(type(watch).__name__, RuntimeError("An earlier genuine failure"))
    before = passes.path.read_bytes()
    with pytest.raises(asyncio.CancelledError):
        await Coordination.watch_worker(watch, lambda: nullcontext(comms), passes)
    assert passes.path.read_bytes() == before
    assert not failed.exists()


@pytest.mark.parametrize("count", (1, 2), ids=("single", "aggregate"))
async def test_cancelled_git_retains_shared_physical_retirement_failure(count: int) -> None:
    children = GitChildren()
    handles: tuple[SpawnedProcess, ...] = ()

    @contextmanager
    def original_authority(child: SpawnedProcess):
        # A real exited original cannot grant observed-process signal authority.
        ObservedProcess(child.identity).require_stop_authority()
        yield

    try:
        with pytest.raises(ChildRetirementFailed) as opening_failure:
            with OwnedGitOpening(children).bound(), Repo.acquiring(), ExitStack() as exchanges:
                for _ in range(count):
                    process = exchanges.enter_context(Repo.opening(
                        (sys.executable, "-c", "pass"), env=os.environ,
                        stdio=DetachedChildStdio(stdout=subprocess.PIPE, stderr=subprocess.PIPE),
                    ))
                    process.communicate(timeout=10)
                    assert process.returncode == 0
                with children.lock:
                    handles = tuple(children.active)
                assert len(handles) == count and all(child.retired for child in handles)
                with pytest.raises(ChildRetirementFailed) as stopped:
                    SynchronousProcess.stop_together(tuple(
                        (child, partial(original_authority, child)) for child in handles))
                failure = stopped.value
                cause = failure.__cause__
                if count == 1:
                    assert isinstance(cause, ProcessLookupError)
                else:
                    assert isinstance(cause, ExceptionGroup)
                    assert len(cause.exceptions) == count
                    assert all(isinstance(error, ProcessLookupError) for error in cause.exceptions)
                # Shutdown sets each acquisition's cancellation, then joins the
                # original failed Futures. Also cancel the joining task itself.
                async def cancelled_shutdown() -> None:
                    shutdown = children.start_shutdown()
                    caller = asyncio.current_task()
                    assert caller is not None
                    caller.cancel()
                    await join_retirement(shutdown)

                with pytest.raises(ChildRetirementFailed) as shutdown_failure:
                    await asyncio.create_task(cancelled_shutdown())
                assert shutdown_failure.value is failure
                with children.lock:
                    permissions = tuple(children.active.values())
                for permission in permissions:
                    with pytest.raises(GitAcquisitionCancelled):
                        permission.completed()
                # Scope exit reaches the same failed Future through GitChildren.retire.
        assert opening_failure.value is failure
        assert all(child in children.active for child in handles)
    finally:
        # These real children already exited. Failed attestation retains their
        # registrations; releasing the test's pipe descriptors is not a stop claim.
        for child in handles:
            child.close_streams()


async def test_owned_git_ordinary_timeout_and_nonzero_keep_their_errors(
    git_preparation: GitPreparation,
) -> None:
    repo = git_preparation.repository
    children = GitChildren()
    with OwnedGitOpening(children).bound():
        with pytest.raises(GitCommandError):
            await AsyncWork.run_async(lambda: Repo.git(repo.checkout, "not-a-git-command"))
    with HeldGitHTTP.serving(git_preparation.source.remote_path, "GET") as remote:
        Repo.git(repo.checkout, "config", "protocol.version", "0")
        Repo.git(repo.checkout, "remote", "set-url", "origin", remote.url)
        with OwnedGitOpening(children).bound():
            with pytest.raises(subprocess.TimeoutExpired):
                await AsyncWork.run_async(lambda: Repo.git(
                    repo.checkout, "ls-remote", "--refs", "origin", timeout=.5))
    assert not children.active and not children.closing.is_set()
