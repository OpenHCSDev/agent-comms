"""A checkout owns its pinned submodules: an interrupted clone is rebuilt, and retiring a
tree with submodules needs no hand-forced removal."""

import shutil
import subprocess
import time
from pathlib import Path

import pytest

from agent_comms.bus import Bus
from agent_comms.change_report import FileLine
from agent_comms.comms import wire
from agent_comms.field_codec import FieldCodec, GoalId
from agent_comms.goals import OrderCheck
from agent_comms.order_runs import MechanicalOrderRun
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.registry_document import RegisteredCommanderProject
from agent_comms.repo import Checkout, CheckoutHoldsWorkError, Repo, ScratchCheckout
from agent_comms.repository_name import RepositoryName
from agent_comms.storage_rules import StorageRule
from agent_comms.thread_execution import ExternalThreadExecution
from agent_comms.threads import Thread
from agent_comms.todos import PendingProblemOrder
from cli_command import core
from fixture_git import IDENTITY
from order_scope_fixture import FIX_SCOPE
from source_revision_fixture import SourceRepository


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ("git", *IDENTITY, "-C", str(path), *args),
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.fixture
def repository(tmp_path, monkeypatch):
    """A repository pinning one submodule from a local source, as ours pins refactor-audit."""
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


@pytest.fixture
def checkout_command_bus(repository, tmp_path, monkeypatch, installed_runtime):
    """A held order and its real pinned-submodule repository, with a local origin."""
    prepare_checkout_goal_id = GoalId.mint()
    (repository / "owner.py").write_text(SourceRepository.main_source)
    git(repository, "commit", "-qam", "Declare the order's source owner")
    git(repository, "branch", "-M", "main")
    identity = RepositoryName("OpenHCSDev", "agent-comms")
    remote = SourceRepository.remote_for(tmp_path, identity)
    git(repository, "remote", "add", "origin", str(remote))
    git(repository, "push", "-q", "origin", "main")
    root = tmp_path / "wire"
    root.mkdir(mode=0o700)
    rule = StorageRule(checkout_base=tmp_path / "checkouts",
                       scratch_base=tmp_path / "scratch", floor_gib=0)
    StorageRule.path_in(root).write_text(FieldCodec.compact(FieldCodec.encode(rule)))
    Bus.install(Bus.path_in(root))
    comms = wire(root)
    lead = comms.registry.declare(Thread(
        "lead", frozenset(), str(tmp_path / "lead"), execution=ExternalThreadExecution))
    worker = comms.registry.declare(Thread(
        "worker", frozenset(), str(repository), execution=ExternalThreadExecution))
    comms.registry.set_commander_project(RegisteredCommanderProject(
        lead.incarnation, (Repo(checkout=repository, github=identity),)))
    request = comms.goals.todos.add_request(lead.incarnation, "Prepare the pinned checkout").id
    now = time.time()
    order = PendingProblemOrder(
        id="prepare-checkout", owner=FileLine("owner.py", 1), scope=FIX_SCOPE,
        run=MechanicalOrderRun(), check=OrderCheck(str(repository), "true"),
        edits=("Create the order checkout through its pinned-submodule owner.",),
        banned=("No checkout creation bypassing its pinned submodules.",),
        report="Report the prepared checkout.", at=RecordedEventTime(now),
        first_call_deadline=RecordedEventTime(now + 120))
    comms.goals.todos.create(
        "P1", identity, "Prepare the pinned checkout", creator=lead, owner=worker,
        assigner=lead.incarnation, generation=prepare_checkout_goal_id, request=request,
        review_at=RecordedEventTime(now + 3600), order=order)
    monkeypatch.setenv("AGENT_COMMS_THREAD", worker.name)
    return comms


def test_checkout_add_prepares_the_order_repository_submodule(checkout_command_bus):
    comms = checkout_command_bus
    result = core("--root", str(comms.root), "checkout", "add", "P1")
    assert result.returncode == 0, result.stderr
    path = comms.registry.storage_rule.order_checkout("worker", "P1")
    assert (path / "pinned" / "catalog.md").read_text() == "patterns\n"


def test_checkout_add_repairs_an_existing_checkout_with_a_missing_store(checkout_command_bus):
    comms = checkout_command_bus
    first = core("--root", str(comms.root), "checkout", "add", "P1")
    assert first.returncode == 0, first.stderr
    checkout = Checkout(comms.registry.storage_rule.order_checkout("worker", "P1"))
    pointer_without_store(checkout)
    repaired = core("--root", str(comms.root), "checkout", "add", "P1")
    assert repaired.returncode == 0, repaired.stderr
    (module,) = checkout.submodules()
    assert module.usable()
    assert (module.path / "catalog.md").read_text() == "patterns\n"


def test_checkout_remove_retires_the_order_checkout_with_its_submodule(
    checkout_command_bus, repository,
):
    comms = checkout_command_bus
    added = core("--root", str(comms.root), "checkout", "add", "P1")
    assert added.returncode == 0, added.stderr
    path = comms.registry.storage_rule.order_checkout("worker", "P1")
    (path / "owner.py").write_text("an unpushed edit\n")
    held = core("--root", str(comms.root), "checkout", "remove", "P1")
    assert held.returncode != 0
    assert "holds uncommitted work" in held.stdout + held.stderr
    assert path.is_dir()
    git(path, "checkout", "--", "owner.py")
    removed = core("--root", str(comms.root), "checkout", "remove", "P1")
    assert removed.returncode == 0, removed.stderr
    assert not path.exists()
    assert str(path) not in git(repository, "worktree", "list")
    assert git(repository, "config", "core.bare").strip() == "false"

def test_checkout_add_allocates_a_declared_sibling_repository_before_it_exists(
    checkout_command_bus, tmp_path,
):
    comms = checkout_command_bus
    sibling = SourceRepository.create(tmp_path / "paired", RepositoryName("OpenHCSDev", "toad"))
    project = comms.registry.snapshot().commander_project.require()
    comms.registry.set_commander_project(RegisteredCommanderProject(
        project.commander, (*project.repos, sibling)))
    rule = comms.registry.storage_rule
    worker = comms.registry.require("worker")
    destination = rule.repository_checkout(worker.name, "P1", sibling.github)
    assert not destination.exists()
    assert rule.owns_checkout(worker, destination, sibling.github)
    assert not rule.owns_checkout(worker, destination, RepositoryName("OpenHCSDev", "textual"))
    result = core("--root", str(comms.root), "repository-checkout", "add", "P1",
                  sibling.github.to_wire())
    assert result.returncode == 0, result.stderr
    assert destination.name == "worker--P1--toad"
    assert Repo.of(destination).github == sibling.github
    assert destination in rule.checkouts_of(worker)


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
    checkout = Checkout.add(repository, tmp_path / "unit--P1", "HEAD", "--detach")
    break_store(checkout)
    with pytest.raises(subprocess.CalledProcessError):
        git(checkout.path, "submodule", "update", "--init", "--recursive", "--checkout")
    checkout.prepare()
    assert (checkout.path / "pinned" / "catalog.md").read_text() == "patterns\n"
    assert git(checkout.path, "diff", "--name-only", "HEAD") == ""


def test_prepare_rebuilds_a_store_with_no_origin_when_the_pin_moves(repository, tmp_path):
    """fork-boundaries at install 34: a store with no remote cannot fetch the moved pin."""
    checkout = Checkout.add(repository, tmp_path / "unit--P1", "HEAD", "--detach")
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
    checkout.prepare()
    assert (module.path / "catalog.md").read_text() == "patterns, indexed\n"
    assert git(checkout.path, "diff", "--name-only", "HEAD") == ""


def test_an_intact_checkout_prepares_without_reaching_its_source(repository, tmp_path):
    checkout = Checkout.add(repository, tmp_path / "unit--P1", "HEAD", "--detach")
    (module,) = checkout.submodules()
    git(checkout.path, "config", f"submodule.{module.name}.url", str(tmp_path / "gone"))
    git(module.path, "remote", "set-url", "origin", str(tmp_path / "gone"))
    checkout.prepare()
    assert (module.path / "catalog.md").read_text() == "patterns\n"


def test_retiring_a_checkout_with_submodules_keeps_held_work_and_drops_scratch(
    repository, tmp_path
):
    held = Checkout.add(repository, tmp_path / "unit--P1", "HEAD", "--detach")
    (held.path / "owner.py").write_text("edited\n")
    with pytest.raises(CheckoutHoldsWorkError):
        held.retire()
    assert held.path.is_dir()
    scratch = ScratchCheckout.add(repository, tmp_path / "check", "HEAD", "--detach")
    (scratch.path / "owner.py").write_text("left by the check\n")
    scratch.retire()
    clean = Checkout.add(repository, tmp_path / "unit--P2", "HEAD", "--detach")
    clean.retire()
    assert not scratch.path.exists() and not clean.path.exists()
    listed = git(repository, "worktree", "list", "--porcelain")
    assert str(held.path.resolve()) in listed
    assert str(scratch.path) not in listed and str(clean.path) not in listed
