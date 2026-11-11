"""Goal authority and Git's declared default drive the real checkout command."""

import subprocess
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from agent_comms.cli_verbs.cli_commands import GoalCheckoutGrant
from agent_comms.comms import Comms
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.planning.goal_actions import ClearGoalAction, SetGoalAction
from agent_comms.planning.goals import Goal
from agent_comms.projects.checkouts import (
    Checkout,
    DetachedStart,
    RepositoryReads,
)
from agent_comms.projects.repo import Repo
from agent_comms.projects.repository_name import RepositoryName
from agent_comms.projects.repository_sources import (
    GitHubRepositorySource,
    LocalRepositorySource,
    RepositorySource,
)
from agent_comms.projects.shell_effects import AcquireRepositoryStorageEffect
from agent_comms.projects.storage_projection import StorageProjection
from agent_comms.projects.storage_rules import StorageRule
from agent_comms.registry.thread_creation import RecordedCreationTime
from agent_comms.registry.threads import ExternalThread, NativeThread, Thread
from agent_comms.transport.tool_denial import StorageDenied
from cli_command import core_process
from source_revision_fixture import SourceRepository


@pytest.mark.parametrize("execution", (NativeThread, ExternalThread), ids=("native", "external"))
def test_goal_checkout_uses_origin_default_and_retires(
    private_comms, tmp_path, monkeypatch, execution,
):
    comms = private_comms
    repository = SourceRepository.create(
        tmp_path / "source", RepositoryName("OpenHCSDev", "OpenMFD"),
    )
    repository.git(repository.checkout, "branch", "-m", "main", "master")
    repository.git(repository.checkout, "push", "origin", "master")
    repository.git(repository.remote_path, "symbolic-ref", "HEAD", "refs/heads/master")
    repository.git(repository.checkout, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
    rule = StorageRule(checkout_base=tmp_path / "checkouts",
                       scratch_base=tmp_path / "scratch", floor_gib=0)
    StorageRule.path_in(comms.root).write_text(FieldCodec.compact(FieldCodec.encode(rule)))
    worker = comms.registry.declare(execution(
        "worker", frozenset(), Checkout(repository.checkout)))
    goal = comms.goals.update_goal(worker.name, SetGoalAction(text="Work in OpenMFD"))
    monkeypatch.delenv("AGENT_COMMS_THREAD", raising=False)
    monkeypatch.delenv("PI_WORKTREE", raising=False)
    command = ("--root", str(comms.root), "goal-checkout", "--from", worker.name)
    denied = core_process(*command, "add", str(repository.checkout))
    assert denied.returncode != 0
    assert "git remote set-head origin --auto" in denied.stdout + denied.stderr
    repository.git(repository.checkout, "remote", "set-head", "origin", "--auto")
    created = core_process(*command, "add", str(repository.checkout))
    assert created.returncode == 0, created.stdout + created.stderr
    path = rule.repository_checkout(rule.roots_of(worker), goal.id, repository.github)
    assert repository.git(path, "rev-parse", "HEAD").strip() == repository.git(
        repository.checkout, "rev-parse", "origin/master",
    ).strip()
    assert path in rule.checkout_paths(comms.registry.require(worker.name), RepositoryReads())
    assert path in rule.checkouts_of(comms.registry.require(worker.name), RepositoryReads())
    prepared = core_process(*command, "add", str(repository.checkout))
    assert prepared.returncode == 0, prepared.stdout + prepared.stderr
    retired = core_process(*command, "remove", str(repository.checkout))
    assert retired.returncode == 0, retired.stdout + retired.stderr
    assert not path.exists()
    assert path not in rule.checkout_paths(comms.registry.require(worker.name), RepositoryReads())


@dataclass(frozen=True)
class RemoteGoalCheckout:
    """The real CLI, registry, remote and donor; Git alone redirects fixture transport."""

    comms: Comms
    repository: SourceRepository
    rule: StorageRule
    worker: Thread
    goal: Goal

    @property
    def source(self) -> GitHubRepositorySource:
        return GitHubRepositorySource(self.repository.github)

    @property
    def path(self) -> Path:
        return self.rule.repository_checkout(self.rule.roots_of(self.worker), self.goal.id,
                                              self.repository.github)

    @property
    def donor(self) -> Path:
        return self.rule.repository_donor(self.worker, self.repository.github)

    def run(self, operation: str = 'add') -> subprocess.CompletedProcess[str]:
        return core_process('--root', str(self.comms.root), 'goal-checkout', '--from',
                            self.worker.name, operation, self.source.to_wire())

    def read(self, source: RepositorySource) -> subprocess.CompletedProcess[str]:
        return core_process(
            '--root', str(self.comms.root), 'tool', '--from', self.worker.name,
            '--tool', 'comms_repository_read',
            '--arguments', FieldCodec.compact({'repository': source.to_wire()}),
        )


@pytest.fixture
def remote_goal_checkout(private_comms: Comms, tmp_path: Path,
                         monkeypatch: pytest.MonkeyPatch) -> RemoteGoalCheckout:
    name = RepositoryName('OpenHCSDev', 'unregistered-remote')
    repository = SourceRepository.create(tmp_path / 'source', name)
    repository.git(repository.checkout, 'branch', '-m', 'main', 'master')
    repository.git(repository.checkout, 'push', 'origin', 'master')
    repository.git(repository.remote_path, 'symbolic-ref', 'HEAD', 'refs/heads/master')
    # An actual local Git transport serves the explicit GitHub URL. No product
    # methods, subprocess results, source identities or acquisition checks are patched.
    for key, value in repository.github_transport_environment().items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv('AGENT_COMMS_THREAD', raising=False)
    monkeypatch.delenv('PI_WORKTREE', raising=False)
    rule = StorageRule(checkout_base=tmp_path / 'checkouts',
                       scratch_base=tmp_path / 'scratch', floor_gib=0)
    StorageRule.path_in(private_comms.root).write_text(FieldCodec.compact(FieldCodec.encode(rule)))
    worker = private_comms.registry.declare(ExternalThread(
        'worker', frozenset(), Checkout(repository.checkout)))
    goal = private_comms.goals.update_goal(worker.name, SetGoalAction(text='Work on the remote'))
    worker = private_comms.registry.require(worker.name)
    return RemoteGoalCheckout(private_comms, repository, rule, worker, goal)


def test_first_remote_goal_checkout_uses_master_and_preserves_its_donor(
    remote_goal_checkout: RemoteGoalCheckout,
) -> None:
    case = remote_goal_checkout
    project = case.comms.registry.snapshot().team_project
    assert case.repository.github not in project.repository_names()
    created = case.run()
    assert created.returncode == 0, created.stdout + created.stderr
    assert case.comms.registry.snapshot().team_project == project
    assert Repo.qualified_github(case.path, case.repository.github).default_branch() == 'master'
    assert Checkout(case.path).common_dir == case.donor / '.git'
    assert case.repository.git(case.path, 'rev-parse', 'HEAD').strip() == case.repository.git(
        case.repository.remote_path, 'rev-parse', 'master').strip()
    projection = StorageProjection(case.rule, case.comms.registry.require(case.worker.name), case.comms)
    projection.require_write(case.path / 'new-source.py')
    with pytest.raises(StorageDenied, match='not allocated editable work'):
        projection.require_write(case.donor / 'new-source.py')
    (case.path / 'retained.py').write_text('retained = True\n')
    repeated = case.run()
    assert repeated.returncode == 0, repeated.stdout + repeated.stderr
    assert (case.path / 'retained.py').read_text() == 'retained = True\n'
    denied = case.run('remove')
    assert denied.returncode != 0
    case.repository.git(case.path, 'add', 'retained.py')
    case.repository.git(case.path, 'commit', '-m', 'Unpublished work stays in the donor')
    commit = case.repository.git(case.path, 'rev-parse', 'HEAD').strip()
    retired = case.run('remove')
    assert retired.returncode == 0, retired.stdout + retired.stderr
    assert not case.path.exists()
    assert case.repository.git(case.donor, 'cat-file', '-t', commit).strip() == 'commit'
    restored = case.run()
    assert restored.returncode == 0, restored.stdout + restored.stderr
    assert case.repository.git(case.path, 'rev-parse', 'HEAD').strip() == commit


@pytest.mark.parametrize('target', ('path', 'donor'))
def test_remote_goal_checkout_refuses_occupied_paths_without_clearing_them(
    remote_goal_checkout: RemoteGoalCheckout, target: str,
) -> None:
    case = remote_goal_checkout
    occupied = {'path': case.path, 'donor': case.donor}[target]
    occupied.mkdir(parents=True)
    marker = occupied / 'keep'
    marker.write_text('not acquisition scratch')
    refused = case.run()
    assert refused.returncode != 0
    assert marker.read_text() == 'not acquisition scratch'
    assert not (case.donor / '.git').exists()


@pytest.mark.parametrize('target', ('path', 'donor', 'donor-parent'))
def test_remote_goal_checkout_refuses_symlink_allocations_before_acquisition(
    remote_goal_checkout: RemoteGoalCheckout, tmp_path: Path, target: str,
) -> None:
    case = remote_goal_checkout
    foreign = tmp_path / 'foreign'
    foreign.mkdir()
    (foreign / 'keep').write_text('untouched')
    link = {'path': case.path, 'donor': case.donor, 'donor-parent': case.donor.parent}[target]
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(foreign, target_is_directory=True)
    refused = case.run()
    assert refused.returncode != 0
    assert 'physical allocation' in refused.stdout + refused.stderr
    assert tuple(foreign.iterdir()) == (foreign / 'keep',)


@pytest.mark.parametrize('origin', (
    'https://github.com/other/unregistered-remote.git',
    'https://not-github.example/openhcsdev/unregistered-remote.git',
))
def test_remote_goal_checkout_refuses_actual_origin_mismatch(
    remote_goal_checkout: RemoteGoalCheckout, origin: str,
) -> None:
    case = remote_goal_checkout
    created = case.run()
    assert created.returncode == 0, created.stdout + created.stderr
    case.repository.git(case.donor, 'config', 'remote.origin.url', origin)
    (case.path / 'keep').write_text('local work')
    refused = case.run()
    assert refused.returncode != 0
    assert 'requested GitHub origin' in refused.stdout + refused.stderr
    assert (case.path / 'keep').read_text() == 'local work'


def test_remote_goal_checkout_cannot_adopt_another_donor_of_the_same_repository(
    remote_goal_checkout: RemoteGoalCheckout, tmp_path: Path,
) -> None:
    case = remote_goal_checkout
    case.source.acquire(case.rule, case.worker)
    foreign = tmp_path / 'retained-donor'
    case.donor.rename(foreign)
    Checkout.add(foreign, case.path, DetachedStart('HEAD'))
    before = case.repository.git(foreign, 'show-ref')
    refused = case.run()
    assert refused.returncode != 0
    assert 'goal-owned donor' in refused.stdout + refused.stderr
    assert case.repository.git(foreign, 'show-ref') == before
    assert Checkout(case.path).common_dir == foreign / '.git'
    assert not case.donor.exists()


@pytest.mark.parametrize('unborn', (False, True))
def test_remote_goal_checkout_preserves_failed_acquisition_without_a_default(
    remote_goal_checkout: RemoteGoalCheckout, unborn: bool,
) -> None:
    case = remote_goal_checkout
    if unborn:
        for branch in ('master', 'main'):
            case.repository.git(case.repository.remote_path, 'update-ref', '-d', f'refs/heads/{branch}')
    else:
        case.repository.git(case.repository.remote_path, 'symbolic-ref', 'HEAD', 'refs/heads/missing')
    failed = case.run()
    assert failed.returncode != 0
    assert str(case.donor) in failed.stdout + failed.stderr
    assert case.donor.is_dir()
    assert not case.path.exists()
    again = case.run()
    assert again.returncode != 0
    assert 'retained unchanged' in again.stdout + again.stderr
    assert not case.path.exists()


def test_remote_goal_checkout_refuses_a_partial_git_store(
    remote_goal_checkout: RemoteGoalCheckout,
) -> None:
    case = remote_goal_checkout
    case.repository.git(case.rule.checkout_base.parent, 'init', str(case.donor))
    case.repository.git(case.donor, 'remote', 'add', 'origin',
                        Repo.github_url(case.repository.github))
    before = (case.donor / '.git' / 'HEAD').read_bytes()
    refused = case.run()
    assert refused.returncode != 0
    assert (case.donor / '.git' / 'HEAD').read_bytes() == before
    assert not case.path.exists()


def test_remote_goal_checkout_refuses_promised_missing_objects_without_fetching_them(
    remote_goal_checkout: RemoteGoalCheckout,
) -> None:
    case = remote_goal_checkout
    case.repository.git(case.repository.remote_path, 'config', 'uploadpack.allowFilter', 'true')
    case.donor.parent.mkdir(parents=True)
    case.repository.git(case.rule.checkout_base.parent, 'clone', '--no-checkout', '--filter=blob:none',
                        case.repository.remote_path.as_uri(), str(case.donor))
    case.repository.git(case.donor, 'remote', 'set-url', 'origin', Repo.github_url(case.repository.github))
    before = case.repository.git(case.donor, 'rev-list', '--objects', '--missing=print', 'HEAD')
    assert any(line.startswith('?') for line in before.splitlines())
    refused = case.run()
    assert refused.returncode != 0
    assert 'retained unchanged' in refused.stdout + refused.stderr
    assert not case.path.exists()
    assert case.repository.git(case.donor, 'rev-list', '--objects', '--missing=print', 'HEAD') == before


@pytest.mark.parametrize('changed', ('goal', 'incarnation'))
def test_acquisition_cannot_publish_for_a_changed_goal_or_incarnation(
    remote_goal_checkout: RemoteGoalCheckout, changed: str,
) -> None:
    case = remote_goal_checkout
    captured = case.worker
    if changed == 'incarnation':
        captured = replace(captured, created_at=RecordedCreationTime(captured.created_at + 1))
    grant = GoalCheckoutGrant(captured, case.goal)
    if changed == 'goal':
        AcquireRepositoryStorageEffect(case.path, case.source).check(
            StorageProjection(case.rule, case.worker, case.comms))
        case.source.acquire(case.rule, case.worker)
        case.comms.goals.update_goal(case.worker.name, SetGoalAction(text='Different goal'))
    with pytest.raises(ValueError):
        grant.revalidate(case.comms)
    assert not case.path.exists()


def test_remote_goal_checkout_keeps_independent_owners_separate(
    remote_goal_checkout: RemoteGoalCheckout,
) -> None:
    case = remote_goal_checkout
    peer = case.comms.registry.declare(ExternalThread(
        'peer', frozenset(), Checkout(case.repository.checkout)))
    goal = case.comms.goals.update_goal(peer.name, SetGoalAction(text='Independent remote work'))
    peer = case.comms.registry.require(peer.name)
    other = RemoteGoalCheckout(case.comms, case.repository, case.rule, peer, goal)
    for owner in (case, other):
        created = owner.run()
        assert created.returncode == 0, created.stdout + created.stderr
    assert case.donor != other.donor
    assert Checkout(case.path).common_dir != Checkout(other.path).common_dir
    with pytest.raises(StorageDenied):
        StorageProjection(case.rule, peer, case.comms).require_write(case.path / 'foreign.py')


def test_remote_goal_checkout_checks_disk_before_clone(remote_goal_checkout: RemoteGoalCheckout) -> None:
    case = remote_goal_checkout
    strict = replace(case.rule, floor_gib=2**40)
    StorageRule.path_in(case.comms.root).write_text(FieldCodec.compact(FieldCodec.encode(strict)))
    refused = case.run()
    assert refused.returncode != 0
    assert 'GiB free' in refused.stdout + refused.stderr
    assert not case.donor.exists()
    assert not case.path.exists()


def test_explicit_remote_spelling_does_not_capture_relative_local_paths(
    remote_goal_checkout: RemoteGoalCheckout, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = remote_goal_checkout
    monkeypatch.chdir(case.repository.checkout.parent)
    local = RepositorySource.from_wire('repository')
    assert local.github == case.repository.github
    assert local.to_wire() == str(case.repository.checkout)
    assert FieldCodec.decode(RepositorySource, 'github:OpenHCSDev/unregistered-remote').to_wire() == case.source.to_wire()


def test_new_goal_reuses_existing_repository_tree_and_donor(remote_goal_checkout):
    case = remote_goal_checkout
    created = case.run()
    assert created.returncode == 0, created.stdout + created.stderr
    original, donor = case.path, case.donor
    (original / 'kept-draft.py').write_text('untouched draft\n')
    branch = case.repository.git(original, 'branch', '--show-current')
    next_goal = case.comms.goals.update_goal(case.worker.name, SetGoalAction(text='Next revision'))
    fresh = case.rule.repository_checkout(case.rule.roots_of(case.worker), next_goal.id,
                                         case.repository.github)
    reused = case.run()
    assert reused.returncode == 0, reused.stdout + reused.stderr
    assert not fresh.exists()
    assert (original / 'kept-draft.py').read_text() == 'untouched draft\n'
    assert case.repository.git(original, 'branch', '--show-current') == branch
    current = case.comms.registry.require(case.worker.name)
    assert case.rule.repository_donor(current, case.repository.github) == donor
    StorageProjection(case.rule, current, case.comms).require_write(original / 'next-change.py')


@pytest.fixture
def repository_read_case(remote_goal_checkout: RemoteGoalCheckout) -> RemoteGoalCheckout:
    case = remote_goal_checkout
    case.comms.goals.update_goal(case.worker.name, ClearGoalAction())
    owner = case.comms.registry.require(case.worker.name)
    assert tuple(owner.goal) == ()
    assert not case.comms.goals.todos.held_by(owner.incarnation)
    return case


def test_repository_read_needs_no_goal_or_order_and_grants_no_edits(
    repository_read_case: RemoteGoalCheckout,
) -> None:
    case = repository_read_case
    project = case.comms.registry.snapshot().team_project
    assert case.repository.github not in project.repository_names()
    acquired = case.read(case.source)
    assert acquired.returncode == 0, acquired.stdout + acquired.stderr
    result = FieldCodec.parse(acquired.stdout)
    assert isinstance(result, dict)
    repository = Repo.from_wire(result['repository'])
    assert repository.checkout == case.donor
    assert repository.github == case.repository.github
    assert (case.donor / 'owner.py').read_text() == case.repository.main_source
    assert case.repository.git(case.donor, 'rev-parse', 'HEAD').strip() == case.repository.git(
        case.repository.remote_path, 'rev-parse', 'master').strip()
    assert not case.path.exists()
    current = case.comms.registry.require(case.worker.name)
    assert tuple(current.goal) == ()
    assert not case.comms.goals.todos.held_by(current.incarnation)
    assert case.comms.registry.snapshot().team_project == project
    with pytest.raises(StorageDenied, match='not allocated editable work'):
        StorageProjection(case.rule, current, case.comms).require_write(case.donor / 'change.py')
    again = case.read(case.source)
    assert again.returncode == 0, again.stdout + again.stderr
    assert FieldCodec.parse(again.stdout) == result


def test_repository_read_follows_origins_new_declared_default(
    repository_read_case: RemoteGoalCheckout,
) -> None:
    case = repository_read_case
    acquired = case.read(case.source)
    assert acquired.returncode == 0, acquired.stdout + acquired.stderr
    case.repository.git(case.repository.checkout, 'push', 'origin', 'feature:refs/heads/feature')
    case.repository.git(case.repository.remote_path, 'symbolic-ref', 'HEAD', 'refs/heads/feature')
    refreshed = case.read(case.source)
    assert refreshed.returncode == 0, refreshed.stdout + refreshed.stderr
    assert case.repository.git(case.donor, 'rev-parse', 'HEAD').strip() == case.repository.branch_commit
    assert Repo.from_origin(case.donor).default_branch() == 'feature'


def test_local_repository_read_keeps_the_explicit_source_branch_and_bytes(
    repository_read_case: RemoteGoalCheckout,
) -> None:
    case = repository_read_case
    case.repository.git(case.repository.checkout, 'checkout', '-b', 'local-reading')
    path = case.repository.checkout / 'owner.py'
    path.write_text('local uncommitted text\n')
    before = case.repository.git(case.repository.checkout, 'status', '--porcelain')
    strict = replace(case.rule, floor_gib=2**40)
    StorageRule.path_in(case.comms.root).write_text(FieldCodec.compact(FieldCodec.encode(strict)))
    acquired = case.read(LocalRepositorySource(case.repository))
    assert acquired.returncode == 0, acquired.stdout + acquired.stderr
    result = FieldCodec.parse(acquired.stdout)
    assert isinstance(result, dict)
    assert Repo.from_wire(result['repository']).checkout == case.repository.checkout
    assert case.repository.git(case.repository.checkout, 'branch', '--show-current').strip() == 'local-reading'
    assert case.repository.git(case.repository.checkout, 'status', '--porcelain') == before
    assert path.read_text() == 'local uncommitted text\n'
    assert not case.donor.exists()


@pytest.mark.parametrize('blocked', ('occupied', 'symlink', 'disk'))
def test_repository_read_keeps_acquisition_safety_without_a_write_allocation(
    repository_read_case: RemoteGoalCheckout, tmp_path: Path, blocked: str,
) -> None:
    case = repository_read_case
    if blocked == 'disk':
        strict = replace(case.rule, floor_gib=2**40)
        StorageRule.path_in(case.comms.root).write_text(FieldCodec.compact(FieldCodec.encode(strict)))
    else:
        occupied = case.donor if blocked == 'occupied' else tmp_path / 'foreign'
        occupied.mkdir(parents=True)
        (occupied / 'keep').write_text('untouched')
        if blocked == 'symlink':
            case.donor.parent.mkdir(parents=True, exist_ok=True)
            case.donor.symlink_to(occupied, target_is_directory=True)
    refused = case.read(case.source)
    assert refused.returncode != 0
    assert not case.path.exists()
    if blocked == 'disk':
        assert 'GiB free' in refused.stdout + refused.stderr
        assert not case.donor.exists()
    else:
        assert str(case.donor) in refused.stdout + refused.stderr
        assert (occupied / 'keep').read_text() == 'untouched'
        assert not (occupied / '.git').exists()


def test_repository_reads_keep_independent_callers_separate(
    repository_read_case: RemoteGoalCheckout,
) -> None:
    case = repository_read_case
    peer = case.comms.registry.declare(ExternalThread(
        'read-peer', frozenset(), Checkout(case.repository.checkout)))
    other = replace(case, worker=peer)
    for reader in (case, other):
        acquired = reader.read(reader.source)
        assert acquired.returncode == 0, acquired.stdout + acquired.stderr
    assert case.donor != other.donor
    for reader in (case, other):
        current = case.comms.registry.require(reader.worker.name)
        assert tuple(current.goal) == ()
        with pytest.raises(StorageDenied):
            StorageProjection(case.rule, current, case.comms).require_write(reader.donor / 'edit.py')
