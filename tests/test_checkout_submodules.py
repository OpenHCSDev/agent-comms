"""A checkout owns its pinned submodules: an interrupted clone is rebuilt, and retiring a
tree with submodules needs no hand-forced removal."""

import shutil
import subprocess
import time
from dataclasses import replace
from pathlib import Path

import pytest

import fixture_git
from agent_comms.cli_verbs.cli_commands import OrderCheckoutGrant
from agent_comms.comms import wire
from agent_comms.messaging.bus import Bus
from agent_comms.nominal.field_codec import FieldCodec, GoalId
from agent_comms.planning.goal_actions import SetGoalAction
from agent_comms.planning.goals import OrderCheck
from agent_comms.planning.order_runs import MechanicalOrderRun
from agent_comms.planning.orders import (
    PendingProblemOrder,
    TodoConflict,
    WithdrawTodo,
)
from agent_comms.projects.change_report import FileLine
from agent_comms.projects.checkouts import (
    Checkout,
    CheckoutHoldsWorkError,
    DetachedStart,
    LaunchCheckout,
    NewBranchStart,
    RepositoryReads,
    ScratchCheckout,
)
from agent_comms.projects.git_errors import GitCommandError
from agent_comms.projects.repo import Repo
from agent_comms.projects.repository_name import RepositoryName
from agent_comms.projects.storage_rules import StorageRule
from agent_comms.registry.registry_document import RegisteredTeamProject
from agent_comms.registry.thread_creation import RecordedCreationTime
from agent_comms.registry.thread_status import StoppedThreadStatus
from agent_comms.registry.threads import ExternalThread, NativeThread
from agent_comms.storage.recorded_time import RecordedEventTime
from agent_comms.transport.tool_denial import StorageDenied
from cli_command import core, core_process
from order_scope_fixture import FIX_SCOPE
from source_revision_fixture import SourceRepository

git = fixture_git.run


@pytest.fixture
def repository(tmp_path, monkeypatch):
    """A repository pinning one submodule from a local source."""
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "protocol.file.allow")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "always")
    package = tmp_path / "package"
    package.mkdir()
    git(package, "init", "-q")
    (package / "catalog.md").write_text("patterns\n")
    git(package, "add", ".")
    git(package, "commit", "-q", "-m", "catalog")
    main = tmp_path / "main"
    main.mkdir()
    git(main, "init", "-q")
    (main / "owner.py").write_text("owner\n")
    git(main, "submodule", "add", "-q", str(package), "pinned")
    git(main, "add", ".")
    git(main, "commit", "-q", "-m", "pin")
    return main


@pytest.fixture(params=(NativeThread, ExternalThread), ids=("native", "external"))
def checkout_command_bus(repository, tmp_path, monkeypatch, installed_runtime, request):
    """Both registered execution kinds use the explicit caller, without native launch state."""
    prepare_checkout_goal_id = GoalId.mint()
    (repository / "owner.py").write_text(SourceRepository.main_source)
    git(repository, "commit", "-qam", "Declare the order's source owner")
    git(repository, "branch", "-M", "main")
    identity = RepositoryName("OpenHCSDev", "agent-comms")
    remote = SourceRepository.remote_for(tmp_path, identity)
    git(repository, "remote", "add", "origin", str(remote))
    git(repository, "push", "-q", "origin", "main")
    git(repository, "remote", "set-head", "origin", "--auto")
    root = tmp_path / "wire"
    root.mkdir(mode=0o700)
    rule = StorageRule(checkout_base=tmp_path / "checkouts",
                       scratch_base=tmp_path / "scratch", floor_gib=0)
    StorageRule.path_in(root).write_text(FieldCodec.compact(FieldCodec.encode(rule)))
    Bus.install(Bus.path_in(root))
    comms = wire(root)
    colleague = comms.registry.declare(ExternalThread(
        "colleague", frozenset(), Checkout(tmp_path / "colleague")))
    worker = comms.registry.declare(request.param(
        "worker", frozenset(), Checkout(repository)))
    comms.registry.set_team_project(RegisteredTeamProject((Repo(checkout=repository, github=identity),)))
    request_id = comms.goals.todos.add_request(colleague.incarnation, "Prepare the pinned checkout").id
    now = time.time()
    order = PendingProblemOrder(
        owner=FileLine("owner.py", 1), scope=FIX_SCOPE,
        run=MechanicalOrderRun(), check=OrderCheck(repository, "true"),
        edits=("Create the order checkout through its pinned-submodule owner.",),
        banned=("No checkout creation bypassing its pinned submodules.",),
        report="Report the prepared checkout.", at=RecordedEventTime(now),
        first_call_deadline=RecordedEventTime(now + 120))
    comms.goals.todos.create(
        "P1", identity, "Prepare the pinned checkout", creator=colleague, owner=worker,
        assigner=colleague.incarnation, generation=prepare_checkout_goal_id, request=request_id,
        review_at=RecordedEventTime(now + 3600), order=order)
    monkeypatch.delenv("AGENT_COMMS_THREAD", raising=False)
    monkeypatch.delenv("PI_WORKTREE", raising=False)
    return comms


def test_retired_checkout_remains_readable_without_qualifying_a_new_launch(tmp_path):
    project = tmp_path / 'retired'
    project.mkdir()
    git(project, 'init', '-q')
    qualified = FieldCodec.decode(LaunchCheckout, str(project))
    assert qualified.path == project
    comms = wire(tmp_path / 'wire')
    comms.registry.declare(NativeThread('retired', frozenset(), Checkout(qualified.path)))
    before = comms.registry.store.rows.mark()
    shutil.rmtree(project)
    reopened = wire(comms.root).registry.require('retired')
    assert reopened.worktree == Checkout(project)
    assert reopened.project_path == project
    assert FieldCodec.encode(reopened.worktree) == str(project)
    assert comms.registry.store.rows.mark() == before
    with pytest.raises(FileNotFoundError):
        FieldCodec.decode(LaunchCheckout, str(project))


@pytest.mark.parametrize('spelling', ('', '.'))
def test_launch_checkout_refuses_empty_or_plain_project(tmp_path, monkeypatch, spelling):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError):
        FieldCodec.decode(LaunchCheckout, spelling)


def test_empty_project_does_not_become_the_callers_checkout(tmp_path, monkeypatch):
    git(tmp_path, 'init', '-q')
    monkeypatch.chdir(tmp_path)
    comms = wire(tmp_path / 'wire')
    result = core('--root', str(comms.root), 'new', '--name', 'empty-project', '--project', '')
    assert result.returncode != 0
    assert 'Project path must not be empty' in result.stdout + result.stderr
    assert 'empty-project' not in comms.registry.snapshot().threads


def test_remote_goal_checkout_uses_the_existing_submodule_prepare_and_retire_owner(
    private_comms, repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = RepositoryName('OpenHCSDev', 'remote-pinned')
    remote = SourceRepository.remote_for(tmp_path, name)
    git(repository, 'branch', '-M', 'master')
    git(repository, 'remote', 'add', 'origin', str(remote))
    git(repository, 'push', 'origin', 'master')
    git(remote, 'symbolic-ref', 'HEAD', 'refs/heads/master')
    for key, value in SourceRepository.of(repository).github_transport_environment().items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv('AGENT_COMMS_THREAD', raising=False)
    monkeypatch.delenv('PI_WORKTREE', raising=False)
    comms = private_comms
    rule = StorageRule(checkout_base=tmp_path / 'checkouts',
                       scratch_base=tmp_path / 'scratch', floor_gib=0)
    StorageRule.path_in(comms.root).write_text(FieldCodec.compact(FieldCodec.encode(rule)))
    worker = comms.registry.declare(ExternalThread(
        'remote-worker', frozenset(), Checkout(repository)))
    goal = comms.goals.update_goal(worker.name, SetGoalAction(text='Prepare remote pinned source'))
    worker = comms.registry.require(worker.name)
    command = ('--root', str(comms.root), 'goal-checkout', '--from', worker.name)
    source = f'github:{name.to_wire()}'
    added = core_process(*command, 'add', source)
    assert added.returncode == 0, added.stdout + added.stderr
    path = rule.repository_checkout(rule.roots_of(worker), goal.id, name)
    checkout = Checkout(path)
    assert (path / 'pinned' / 'catalog.md').read_text() == 'patterns\n'
    pointer_without_store(checkout)
    repaired = core_process(*command, 'add', source)
    assert repaired.returncode == 0, repaired.stdout + repaired.stderr
    module, = checkout.submodules()
    assert module.usable()
    retired = core_process(*command, 'remove', source)
    assert retired.returncode == 0, retired.stdout + retired.stderr
    assert not path.exists()
    donor = rule.repository_donor(worker, name)
    assert Repo.qualified_github(donor, name).default_branch() == 'master'


def test_checkout_add_prepares_the_order_repository_submodule(checkout_command_bus):
    comms = checkout_command_bus
    result = core_process("--root", str(comms.root), "checkout", "--from", "worker", "add", "P1")
    assert result.returncode == 0, result.stderr
    rule = comms.registry.storage_rule
    path = rule.order_checkout(rule.initial_roots("worker"), "P1")
    assert (path / "pinned" / "catalog.md").read_text() == "patterns\n"


def test_checkout_add_reuses_dirty_initialized_modules(checkout_command_bus):
    comms = checkout_command_bus
    command = ('--root', str(comms.root), 'checkout', '--from', 'worker', 'add', 'P1')
    first = core_process(*command)
    assert first.returncode == 0, first.stderr
    worker = comms.registry.require('worker')
    path = comms.registry.storage_rule.order_checkout(
        comms.registry.storage_rule.roots_of(worker), 'P1')
    module, = Checkout(path).submodules()
    (module.path / 'catalog.md').write_text('local revision\n')
    git(module.path, 'commit', '-qam', 'Retain local module work')
    head = git(module.path, 'rev-parse', 'HEAD')
    (module.path / 'catalog.md').write_text('dirty module work\n')
    (module.path / 'untracked.md').write_text('not a clone artifact\n')
    reused = core_process(*command)
    assert reused.returncode == 0, reused.stdout + reused.stderr
    assert git(module.path, 'rev-parse', 'HEAD') == head
    assert (module.path / 'catalog.md').read_text() == 'dirty module work\n'
    assert (module.path / 'untracked.md').read_text() == 'not a clone artifact\n'


def test_checkout_add_repairs_an_existing_checkout_with_a_missing_store(checkout_command_bus):
    comms = checkout_command_bus
    first = core_process("--root", str(comms.root), "checkout", "--from", "worker", "add", "P1")
    assert first.returncode == 0, first.stderr
    rule = comms.registry.storage_rule
    checkout = Checkout(rule.order_checkout(rule.initial_roots("worker"), "P1"))
    pointer_without_store(checkout)
    repaired = core_process("--root", str(comms.root), "checkout", "--from", "worker", "add", "P1")
    assert repaired.returncode == 0, repaired.stderr
    (module,) = checkout.submodules()
    assert module.usable()
    assert (module.path / "catalog.md").read_text() == "patterns\n"


def test_checkout_remove_retires_the_order_checkout_with_its_submodule(
    checkout_command_bus, repository,
):
    comms = checkout_command_bus
    added = core_process("--root", str(comms.root), "checkout", "--from", "worker", "add", "P1")
    assert added.returncode == 0, added.stderr
    rule = comms.registry.storage_rule
    path = rule.order_checkout(rule.initial_roots("worker"), "P1")
    # The holder may close the problem before retiring its checkout.
    current = comms.goals.todos.get("P1")
    comms.goals.todos.apply("P1", WithdrawTodo(
        current.revision, comms.registry.require("worker").incarnation,
        "The order is closed before its teammate retires the checkout it worked in."))
    (path / "owner.py").write_text("an unpushed edit\n")
    held = core_process("--root", str(comms.root), "checkout", "--from", "worker", "remove", "P1")
    assert held.returncode != 0
    assert "holds uncommitted work" in held.stdout + held.stderr
    assert path.is_dir()
    git(path, "checkout", "--", "owner.py")
    removed = core_process("--root", str(comms.root), "checkout", "--from", "worker", "remove", "P1")
    assert removed.returncode == 0, removed.stderr
    assert not path.exists()
    assert str(path) not in git(repository, "worktree", "list")
    assert git(repository, "config", "core.bare").strip() == "false"

def test_checkout_add_reopens_retired_order_branch_with_its_commits(checkout_command_bus):
    comms = checkout_command_bus
    rule = comms.registry.storage_rule
    path = rule.order_checkout(rule.initial_roots("worker"), "P1")
    added = core_process("--root", str(comms.root), "checkout", "--from", "worker", "add", "P1")
    assert added.returncode == 0, added.stderr
    (path / "unit-change.py").write_text("kept work\n")
    git(path, "add", "unit-change.py")
    git(path, "commit", "-q", "-m", "Keep the teammate's work")
    commit = git(path, "rev-parse", "HEAD").strip()
    branch = git(path, "branch", "--show-current").strip()

    removed = core_process("--root", str(comms.root), "checkout", "--from", "worker", "remove", "P1")
    assert removed.returncode == 0, removed.stderr
    assert not path.exists()
    restored = core_process("--root", str(comms.root), "checkout", "--from", "worker", "add", "P1")
    assert restored.returncode == 0, restored.stderr
    assert git(path, "rev-parse", "HEAD").strip() == commit
    assert git(path, "branch", "--show-current").strip() == branch
    assert (path / "unit-change.py").read_text() == "kept work\n"
    assert (path / "pinned" / "catalog.md").read_text() == "patterns\n"


def test_checkout_add_allocates_a_declared_sibling_repository_before_it_exists(
    checkout_command_bus, tmp_path,
):
    comms = checkout_command_bus
    sibling = SourceRepository.create(tmp_path / "paired", RepositoryName("OpenHCSDev", "toad"))
    project = comms.registry.snapshot().team_project.require()
    comms.registry.set_team_project(RegisteredTeamProject((*project.repos, sibling)))
    rule = comms.registry.storage_rule
    worker = comms.registry.require("worker")
    destination = rule.repository_checkout(rule.initial_roots(worker.name), "P1", sibling.github)
    assert not destination.exists()
    assert rule.owns_checkout(worker, destination, sibling.github, RepositoryReads())
    assert not rule.owns_checkout(worker, destination, RepositoryName("OpenHCSDev", "textual"), RepositoryReads())
    command = ("--root", str(comms.root), "repository-checkout", "--from", "worker")
    result = core_process(*command, "add", "P1", sibling.github.to_wire())
    assert result.returncode == 0, result.stderr
    assert destination.name == "worker--P1--toad"
    assert Repo.of(destination).github == sibling.github
    assert destination in rule.checkouts_of(worker, RepositoryReads())
    repaired = core_process(*command, "add", "P1", sibling.github.to_wire())
    assert repaired.returncode == 0, repaired.stderr
    retired = core_process(*command, "remove", "P1", sibling.github.to_wire())
    assert retired.returncode == 0, retired.stderr
    assert not destination.exists()
    assert destination not in rule.checkouts_of(worker, RepositoryReads())


@pytest.mark.parametrize("verb,arguments", (
    ("checkout", ("P1",)),
    ("repository-checkout", ("P1", "OpenHCSDev/agent-comms")),
    ("goal-checkout", ()),
))
@pytest.mark.parametrize("caller", ((), ("--from", "unknown"), ("--from", "colleague")))
def test_checkout_refuses_missing_unknown_or_non_holder_before_git_mutation(
    checkout_command_bus, repository, caller, verb, arguments,
):
    comms = checkout_command_bus
    before = git(repository, "show-ref"), git(repository, "worktree", "list", "--porcelain")
    arguments = arguments or (str(repository),)
    result = core_process("--root", str(comms.root), verb, *caller, "add", *arguments)
    assert result.returncode != 0, result.stdout
    assert (git(repository, "show-ref"), git(repository, "worktree", "list", "--porcelain")) == before
    assert not tuple(comms.registry.storage_rule.checkout_base.glob("*"))


def test_checkout_grant_rejects_stale_incarnation_and_generation(checkout_command_bus, repository):
    comms = checkout_command_bus
    worker = comms.registry.require("worker")
    current = comms.goals.todos.get("P1")
    before = git(repository, "show-ref"), git(repository, "worktree", "list", "--porcelain")
    stale = replace(worker, created_at=RecordedCreationTime(worker.created_at + 1))
    with pytest.raises(TodoConflict):
        OrderCheckoutGrant(stale, current).require()
    with pytest.raises(TodoConflict):
        current.require_assignee(worker.incarnation, GoalId.mint())
    assert (git(repository, "show-ref"), git(repository, "worktree", "list", "--porcelain")) == before
    assert not tuple(comms.registry.storage_rule.checkout_base.glob("*"))


def test_checkout_remove_cannot_retire_another_callers_allocation(checkout_command_bus, repository):
    comms = checkout_command_bus
    added = core_process("--root", str(comms.root), "checkout", "--from", "worker", "add", "P1")
    assert added.returncode == 0, added.stderr
    before = git(repository, "worktree", "list", "--porcelain")
    denied = core_process("--root", str(comms.root), "checkout", "--from", "colleague", "remove", "P1")
    assert denied.returncode != 0
    assert git(repository, "worktree", "list", "--porcelain") == before
    rule = comms.registry.storage_rule
    assert rule.order_checkout(rule.initial_roots("worker"), "P1").is_dir()


def config_only_store(checkout: Checkout) -> None:
    """What a clone stopped after git wrote the store's config and before it fetched."""
    (module,) = checkout.submodules()
    shutil.rmtree(module.store)
    module.store.mkdir(parents=True)
    (module.store / "config").write_text("[core]\n\tbare = false\n")


def pointer_without_store(checkout: Checkout) -> None:
    """The working directory's .git still points at a store that no longer exists."""
    (module,) = checkout.submodules()
    shutil.rmtree(module.store)


def hand_copied_directory(checkout: Checkout) -> None:
    """The catalog's files copied into the submodule path, with no store and no .git."""
    (module,) = checkout.submodules()
    shutil.rmtree(module.store)
    shutil.rmtree(module.path)
    module.path.mkdir()
    (module.path / "catalog.md").write_text("copied by hand\n")


def linked_directory(checkout: Checkout) -> None:
    """A link to another tree's catalog standing where the clone belongs."""
    (module,) = checkout.submodules()
    elsewhere = checkout.path.parent / "elsewhere-catalog"
    shutil.copytree(module.path, elsewhere, ignore=shutil.ignore_patterns(".git"))
    shutil.rmtree(module.store)
    shutil.rmtree(module.path)
    module.path.symlink_to(elsewhere)


@pytest.mark.parametrize(
    "break_store",
    (config_only_store, pointer_without_store, hand_copied_directory, linked_directory),
)
def test_prepare_rebuilds_a_store_an_interrupted_clone_left_broken(
    repository, tmp_path, break_store
):
    checkout = Checkout.add(repository, tmp_path / "unit--P1", DetachedStart("HEAD"))
    break_store(checkout)
    with pytest.raises(subprocess.CalledProcessError):
        git(checkout.path, "submodule", "update", "--init", "--recursive", "--checkout")
    checkout.prepare()
    assert (checkout.path / "pinned" / "catalog.md").read_text() == "patterns\n"
    assert git(checkout.path, "diff", "--name-only", "HEAD") == ""


@pytest.mark.parametrize('remove_origin', (False, True), ids=('origin', 'no-origin'))
def test_prepare_preserves_initialized_work_and_initializes_a_missing_sibling(
    repository, tmp_path, remove_origin,
):
    package = tmp_path / 'package'
    git(repository, 'submodule', 'add', '-q', str(package), 'missing')
    git(repository, 'commit', '-qam', 'Add another pinned module')
    checkout = Checkout.add(repository, tmp_path / 'unit--P1', DetachedStart('HEAD'))
    module, missing = checkout.submodules()
    (module.path / 'catalog.md').write_text('local commit\n')
    (module.path / 'deleted.md').write_text('tracked file\n')
    git(module.path, 'add', '.')
    git(module.path, 'commit', '-qm', 'Local module revision')
    git(module.path, 'switch', '-qc', 'local-work')
    (module.path / 'catalog.md').write_text('staged change\n')
    git(module.path, 'add', 'catalog.md')
    (module.path / 'catalog.md').write_text('unstaged change\n')
    (module.path / 'deleted.md').unlink()
    (module.path / 'untracked.md').write_text('untracked work\n')
    if remove_origin:
        git(module.path, 'remote', 'remove', 'origin')
    before = (git(module.path, 'rev-parse', 'HEAD'), git(module.path, 'symbolic-ref', 'HEAD'),
              git(module.path, 'diff', '--binary'), git(module.path, 'diff', '--cached', '--binary'),
              git(module.path, 'status', '--porcelain'))
    git(missing.path, 'branch', 'retained-local-ref')
    git(checkout.path, 'submodule', 'deinit', '-f', '--', 'missing')
    checkout.prepare()
    checkout.prepare()
    assert before == (git(module.path, 'rev-parse', 'HEAD'), git(module.path, 'symbolic-ref', 'HEAD'),
                      git(module.path, 'diff', '--binary'), git(module.path, 'diff', '--cached', '--binary'),
                      git(module.path, 'status', '--porcelain'))
    assert (module.path / 'untracked.md').read_text() == 'untracked work\n'
    assert (missing.path / 'catalog.md').read_text() == 'patterns\n'
    assert git(missing.path, 'rev-parse', 'retained-local-ref') == git(package, 'rev-parse', 'HEAD')
    if remove_origin:
        assert not module.usable()


def test_prepare_uses_the_preserved_parent_head_for_nested_modules(repository, tmp_path):
    package = tmp_path / 'package'
    child = tmp_path / 'child'
    child.mkdir()
    git(child, 'init', '-q')
    (child / 'child.md').write_text('child pin\n')
    git(child, 'add', '.')
    git(child, 'commit', '-qm', 'Child pin')
    for name in ('nested', 'missing'):
        git(package, 'submodule', 'add', '-q', str(child), name)
    git(package, 'commit', '-qam', 'Declare nested modules')
    checkout = Checkout.add(repository, tmp_path / 'unit--P1', DetachedStart('HEAD'))
    module, = checkout.submodules()
    git(module.path, 'fetch', 'origin')
    git(module.path, 'checkout', '-q', git(package, 'rev-parse', 'HEAD').strip())
    parent_head = git(module.path, 'rev-parse', 'HEAD')
    checkout.prepare()
    nested, missing = Checkout(module.path).submodules()
    (nested.path / 'child.md').write_text('local child revision\n')
    git(nested.path, 'commit', '-qam', 'Child work')
    nested_head = git(nested.path, 'rev-parse', 'HEAD')
    (nested.path / 'child.md').write_text('dirty child\n')
    (nested.path / 'untracked.md').write_text('child notes\n')
    git(module.path, 'submodule', 'deinit', '-f', '--', 'missing')
    shutil.rmtree(missing.store)
    checkout.prepare()
    assert git(module.path, 'rev-parse', 'HEAD') == parent_head
    assert git(nested.path, 'rev-parse', 'HEAD') == nested_head
    assert (nested.path / 'child.md').read_text() == 'dirty child\n'
    assert (nested.path / 'untracked.md').read_text() == 'child notes\n'
    assert (missing.path / 'child.md').read_text() == 'child pin\n'


@pytest.mark.parametrize('remove_origin', (False, True), ids=('origin', 'no-origin'))
def test_explicit_pin_update_refuses_to_overwrite_dirty_work(repository, tmp_path, remove_origin):
    checkout = Checkout.add(repository, tmp_path / 'unit--P1', DetachedStart('HEAD'))
    module, = checkout.submodules()
    head = git(module.path, 'rev-parse', 'HEAD')
    package = tmp_path / 'package'
    (package / 'catalog.md').write_text('next pin\n')
    git(package, 'commit', '-qam', 'New pin')
    moved = git(package, 'rev-parse', 'HEAD').strip()
    git(checkout.path, 'update-index', '--cacheinfo', f'160000,{moved},pinned')
    git(checkout.path, 'commit', '-qm', 'Select new module pin')
    (module.path / 'catalog.md').write_text('dirty work\n')
    (module.path / 'untracked.md').write_text('untracked work\n')
    if remove_origin:
        git(module.path, 'remote', 'remove', 'origin')
    with pytest.raises(GitCommandError):
        checkout.update_submodules()
    assert git(module.path, 'rev-parse', 'HEAD') == head
    assert (module.path / 'catalog.md').read_text() == 'dirty work\n'
    assert (module.path / 'untracked.md').read_text() == 'untracked work\n'


def test_explicit_update_restores_a_missing_origin_when_the_pin_moves(repository, tmp_path):
    """fork-boundaries at install 34: a store with no remote cannot fetch the moved pin."""
    checkout = Checkout.add(repository, tmp_path / "unit--P1", DetachedStart("HEAD"))
    (module,) = checkout.submodules()
    git(module.path, "remote", "remove", "origin")
    package = tmp_path / "package"
    (package / "catalog.md").write_text("patterns, indexed\n")
    git(package, "commit", "-q", "-am", "index")
    moved = git(package, "rev-parse", "HEAD").strip()
    git(checkout.path, "update-index", "--cacheinfo", f"160000,{moved},pinned")
    git(checkout.path, "commit", "-q", "-m", "move the pin")
    with pytest.raises(subprocess.CalledProcessError):
        git(checkout.path, "submodule", "update", "--init", "--recursive", "--checkout")
    checkout.update_submodules()
    assert (module.path / "catalog.md").read_text() == "patterns, indexed\n"
    assert git(checkout.path, "diff", "--name-only", "HEAD") == ""


def test_an_intact_checkout_prepares_without_reaching_its_source(repository, tmp_path):
    checkout = Checkout.add(repository, tmp_path / "unit--P1", DetachedStart("HEAD"))
    (module,) = checkout.submodules()
    git(checkout.path, "config", f"submodule.{module.name}.url", str(tmp_path / "gone"))
    git(module.path, "remote", "set-url", "origin", str(tmp_path / "gone"))
    checkout.prepare()
    assert (module.path / "catalog.md").read_text() == "patterns\n"


def test_retiring_a_checkout_with_submodules_keeps_held_work_and_drops_scratch(
    repository, tmp_path
):
    held = Checkout.add(repository, tmp_path / "unit--P1", DetachedStart("HEAD"))
    (held.path / "owner.py").write_text("edited\n")
    with pytest.raises(CheckoutHoldsWorkError):
        held.retire()
    assert held.path.is_dir()
    scratch = ScratchCheckout.add(repository, tmp_path / "check", DetachedStart("HEAD"))
    (scratch.path / "owner.py").write_text("left by the check\n")
    scratch.retire()
    clean = Checkout.add(repository, tmp_path / "unit--P2", DetachedStart("HEAD"))
    clean.retire()
    assert not scratch.path.exists() and not clean.path.exists()
    listed = git(repository, "worktree", "list", "--porcelain")
    assert str(held.path.resolve()) in listed
    assert str(scratch.path) not in listed and str(clean.path) not in listed


@pytest.fixture
def retained_transfer(checkout_command_bus, repository):
    comms = checkout_command_bus
    worker = comms.registry.require('worker')
    rule = comms.registry.storage_rule
    source = rule.order_checkout(rule.roots_of(worker), 'P4')
    source.parent.mkdir(parents=True, exist_ok=True)
    checkout = Checkout.add(repository, source, NewBranchStart('retained-transfer', 'HEAD'))
    comms.registry.register(replace(worker, worktree=checkout), StoppedThreadStatus())
    (source / 'owner.py').write_text('staged bytes\n')
    git(source, 'add', 'owner.py')
    (source / 'owner.py').write_text('unstaged bytes\n')
    (source / 'untracked.bin').write_bytes(b'\x00retained\xff')
    (source / 'pinned' / 'catalog.md').write_text('dirty submodule\n')
    destination = rule.order_checkout(rule.roots_of(worker), 'P1')
    return comms, checkout, destination


def test_public_transfer_keeps_dirty_index_branch_modules_and_current_thread(retained_transfer, repository):
    comms, source, destination = retained_transfer
    original = comms.registry.require('worker')
    branch = git(source.path, 'branch', '--show-current')
    head = git(source.path, 'rev-parse', 'HEAD')
    status = git(source.path, 'status', '--porcelain')
    index = Path(git(source.path, 'rev-parse', '--path-format=absolute', '--git-path', 'index').strip())
    index_bytes = index.read_bytes()
    shared_config = (repository / '.git' / 'config').read_bytes()
    arguments = ('--root', str(comms.root), 'checkout', '--from', 'worker', 'transfer',
                 'P1', '--source', str(source.path))
    result = core_process(*arguments)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not source.path.exists()
    assert git(destination, 'branch', '--show-current') == branch
    assert git(destination, 'rev-parse', 'HEAD') == head
    assert index.read_bytes() == index_bytes
    assert git(destination, 'status', '--porcelain') == status
    assert (destination / 'owner.py').read_text() == 'unstaged bytes\n'
    assert (destination / 'untracked.bin').read_bytes() == b'\x00retained\xff'
    assert (destination / 'pinned' / 'catalog.md').read_text() == 'dirty submodule\n'
    assert git(destination / 'pinned', 'rev-parse', '--show-toplevel').strip() == str(destination / 'pinned')
    assert (repository / '.git' / 'config').read_bytes() == shared_config
    current = comms.registry.require('worker')
    assert current.worktree == Checkout(destination)
    assert current.incarnation == original.incarnation and current.goal == original.goal
    assert comms.goals.todos.get('P1').held().owner_identity == original.incarnation
    repeated = core_process(*arguments)
    assert repeated.returncode == 0, repeated.stdout + repeated.stderr
    assert git(destination, 'rev-parse', 'HEAD') == head


@pytest.mark.parametrize('refusal', ('occupied', 'foreign', 'stale'))
def test_transfer_refuses_before_moving_retained_work(retained_transfer, refusal):
    comms, source, destination = retained_transfer
    worker = comms.registry.require('worker')
    repository = comms.goals.todos.get('P1').repository
    if refusal == 'occupied':
        destination.mkdir()
    elif refusal == 'foreign':
        comms.registry.declare(ExternalThread('another-owner', frozenset(), source))
    else:
        current = comms.goals.todos.get('P1')
        stale = OrderCheckoutGrant(worker, current)
        comms.goals.todos.apply('P1', WithdrawTodo(
            current.revision, worker.incarnation,
            why='The target assignment ends before the prepared transfer runs.'))
        with pytest.raises(ValueError, match='changed before checkout transfer'):
            stale.require_transfer(comms, repository)
        assert source.path.exists() and not destination.exists()
        return
    before = git(source.path, 'status', '--porcelain'), git(source.path, 'rev-parse', 'HEAD')
    result = core_process('--root', str(comms.root), 'checkout', '--from', 'worker',
                          'transfer', 'P1', '--source', str(source.path))
    assert result.returncode != 0
    assert source.path.exists()
    assert (git(source.path, 'status', '--porcelain'), git(source.path, 'rev-parse', 'HEAD')) == before


def test_transfer_refuses_current_reviewer_then_keeps_departed_reviewers_history(
    retained_transfer, repository,
):
    comms, source, destination = retained_transfer
    reviewer = comms.registry.declare(ExternalThread(
        'reviewer', frozenset(), source))
    rule = comms.registry.storage_rule
    reviewer_path = rule.order_checkout(rule.roots_of(reviewer), 'P2')
    Checkout.add(repository, reviewer_path, NewBranchStart('reviewer', 'HEAD'))
    before = git(source.path, 'rev-parse', 'HEAD'), git(source.path, 'status', '--porcelain')
    arguments = ('--root', str(comms.root), 'checkout', '--from', 'worker',
                 'transfer', 'P1', '--source', str(source.path))
    refused = core_process(*arguments)
    assert refused.returncode != 0
    assert source.path.exists() and not destination.exists()
    assert (git(source.path, 'rev-parse', 'HEAD'),
            git(source.path, 'status', '--porcelain')) == before

    assert comms.threads.set_project(reviewer.incarnation, str(reviewer_path)).changed
    departed = comms.registry.require(reviewer.name)
    assert departed.incarnation == reviewer.incarnation
    assert departed.worktree == Checkout(reviewer_path)
    assert source in departed.earlier_projects
    assert not rule.owns_present_checkout(departed, source.path, RepositoryReads())
    moved = core_process(*arguments)
    assert moved.returncode == 0, moved.stdout + moved.stderr
    assert not source.path.exists()
    assert (git(destination, 'rev-parse', 'HEAD'),
            git(destination, 'status', '--porcelain')) == before
    assert (destination / 'untracked.bin').read_bytes() == b'\x00retained\xff'
    assert comms.registry.require(reviewer.name) == departed


def test_transfer_refuses_foreign_allocation_even_after_its_owner_changes_project(
    retained_transfer, repository,
):
    comms, source, destination = retained_transfer
    rule = comms.registry.storage_rule
    worker = comms.registry.require('worker')
    assert rule.owns_present_checkout(worker, source.path, RepositoryReads())
    worker_path = rule.order_checkout(rule.roots_of(worker), 'P2')
    Checkout.add(repository, worker_path, NewBranchStart('worker-next', 'HEAD'))
    reviewer = comms.registry.declare(ExternalThread(
        'reviewer', frozenset(), source))
    reviewer_path = rule.order_checkout(rule.roots_of(reviewer), 'P3')
    Checkout.add(repository, reviewer_path, NewBranchStart('reviewer-next', 'HEAD'))
    assert comms.threads.set_project(worker.incarnation, str(worker_path)).changed
    assert comms.threads.set_project(reviewer.incarnation, str(reviewer_path)).changed
    worker = comms.registry.require(worker.name)
    reviewer = comms.registry.require(reviewer.name)
    assert worker.worktree == Checkout(worker_path)
    assert source in worker.earlier_projects and source in reviewer.earlier_projects
    assert rule.owns_present_checkout(worker, source.path, RepositoryReads())
    assert not rule.owns_present_checkout(reviewer, source.path, RepositoryReads())
    before = git(source.path, 'rev-parse', 'HEAD'), git(source.path, 'status', '--porcelain')
    with pytest.raises(StorageDenied, match='worker'):
        rule.require_transfer_source(
            reviewer, source.path, comms.registry.snapshot().threads.values(),
            comms.goals.todos.get('P1').repository)
    assert source.path.exists() and not destination.exists()
    assert (git(source.path, 'rev-parse', 'HEAD'),
            git(source.path, 'status', '--porcelain')) == before


def test_transfer_accepts_callers_historical_launch_without_current_allocation(
    checkout_command_bus, repository,
):
    comms = checkout_command_bus
    rule = comms.registry.storage_rule
    worker = comms.registry.require('worker')
    source = rule.checkout_base / 'historical-launch'
    source.parent.mkdir(parents=True, exist_ok=True)
    checkout = Checkout.add(repository, source, NewBranchStart('historical', 'HEAD'))
    next_project = rule.order_checkout(rule.roots_of(worker), 'P2')
    Checkout.add(repository, next_project, NewBranchStart('historical-next', 'HEAD'))
    with pytest.raises(StorageDenied, match='not a current or historical checkout'):
        rule.require_transfer_source(
            worker, source, comms.registry.snapshot().threads.values(),
            comms.goals.todos.get('P1').repository)
    assert comms.threads.set_project(worker.incarnation, str(source)).changed
    assert comms.threads.set_project(worker.incarnation, str(next_project)).changed
    worker = comms.registry.require(worker.name)
    assert checkout in worker.earlier_projects
    assert not rule.owns_present_checkout(worker, source, RepositoryReads())
    comms.registry.register(worker, StoppedThreadStatus())
    destination = rule.order_checkout(rule.roots_of(worker), 'P1')
    (source / 'owner.py').write_text('historical dirty bytes\n')
    before = git(source, 'branch', '--show-current'), git(source, 'rev-parse', 'HEAD')
    moved = core_process('--root', str(comms.root), 'checkout', '--from', worker.name,
                         'transfer', 'P1', '--source', str(source))
    assert moved.returncode == 0, moved.stdout + moved.stderr
    assert not source.exists()
    assert (git(destination, 'branch', '--show-current'),
            git(destination, 'rev-parse', 'HEAD')) == before
    assert (destination / 'owner.py').read_text() == 'historical dirty bytes\n'
    current = comms.registry.require(worker.name)
    assert current.worktree == Checkout(next_project)
    assert Checkout(destination) in current.earlier_projects


def test_transfer_restores_original_tree_and_modules_after_actual_git_failure(repository, tmp_path):
    source = Checkout.add(repository, tmp_path / 'before', NewBranchStart('failed-transfer', 'HEAD'))
    destination = tmp_path / 'after'
    (source.path / 'owner.py').write_text('keep after failure\n')
    (source.path / 'pinned' / 'catalog.md').write_text('keep module after failure\n')
    before = git(source.path, 'rev-parse', 'HEAD'), git(source.path, 'status', '--porcelain')
    config = (repository / '.git' / 'config').read_bytes()
    with pytest.raises(GitCommandError, match='not-a-git-command'):
        with source.relocating(destination):
            assert not source.path.exists()
            assert git(destination / 'pinned', 'rev-parse', '--show-toplevel').strip() == str(destination / 'pinned')
            Repo.git(destination, 'not-a-git-command')
    assert source.path.exists() and not destination.exists()
    assert (git(source.path, 'rev-parse', 'HEAD'), git(source.path, 'status', '--porcelain')) == before
    assert (source.path / 'pinned' / 'catalog.md').read_text() == 'keep module after failure\n'
    assert git(source.path / 'pinned', 'rev-parse', '--show-toplevel').strip() == str(source.path / 'pinned')
    assert (repository / '.git' / 'config').read_bytes() == config


@pytest.mark.parametrize('allocated', (False, True), ids=('selected-history', 'mixed-case-allocation'))
def test_exact_historical_retirement_needs_ownership_not_a_current_goal(
        checkout_command_bus, repository, allocated):
    comms = checkout_command_bus
    rule = comms.registry.storage_rule
    owner = comms.registry.require('worker')
    path = rule.checkout_base / (
        f'{owner.name}--{GoalId.mint()}--Agent-Comms' if allocated
        else 'retained-historical-source')
    path.parent.mkdir(parents=True, exist_ok=True)
    Checkout.add(repository, path, NewBranchStart('retained-old-source', 'HEAD'))
    if not allocated:
        assert comms.threads.set_project(owner.incarnation, str(path)).changed
        assert comms.threads.set_project(owner.incarnation, str(repository)).changed
    owner = comms.registry.require(owner.name)
    assert (Checkout(path) in owner.earlier_projects) is (not allocated)
    assert (path in rule.checkouts_of(owner, RepositoryReads())) is allocated
    foreign = core_process('--root', str(comms.root), 'goal-checkout', '--from', 'colleague',
                           '--source', str(path), 'remove', str(repository))
    assert foreign.returncode != 0 and path.exists()
    module_source = path / 'pinned' / 'catalog.md'
    module_source.write_text('unpublished module edit\n')
    blocked = core_process('--root', str(comms.root), 'goal-checkout', '--from', owner.name,
                           '--source', str(path), 'remove', str(repository))
    assert blocked.returncode != 0 and module_source.read_text() == 'unpublished module edit\n'
    git(module_source.parent, 'restore', 'catalog.md')
    # Retirement remains available even when new allocations are refused by disk pressure.
    strict = replace(rule, floor_gib=2**40)
    StorageRule.path_in(comms.root).write_text(FieldCodec.compact(FieldCodec.encode(strict)))
    retired = core_process('--root', str(comms.root), 'goal-checkout', '--from', owner.name,
                           '--source', str(path), 'remove', str(repository))
    assert retired.returncode == 0, retired.stdout + retired.stderr
    assert not path.exists()
