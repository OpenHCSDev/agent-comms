"""A checkout owns its pinned submodules: an interrupted clone is rebuilt, and retiring a
tree with submodules needs no hand-forced removal."""
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_comms.repo import Checkout, CheckoutHoldsWork, ReadableCheckout, ScratchCheckout


def git(path: Path, *args: str) -> str:
    return subprocess.run(("git", "-C", str(path), "-c", "user.name=t", "-c", "user.email=t@t",
                           *args), check=True, capture_output=True, text=True).stdout


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


@pytest.mark.parametrize("break_store", (config_only_store, pointer_without_store))
def test_prepare_rebuilds_a_store_an_interrupted_clone_left_broken(repository, tmp_path,
                                                                    break_store):
    checkout = Checkout.add(repository, tmp_path / "unit--P1", "HEAD", "--detach")
    break_store(checkout)
    with pytest.raises(subprocess.CalledProcessError):
        git(checkout.path, "submodule", "update", "--init", "--recursive", "--checkout")
    assert checkout.read() == ReadableCheckout(checkout.path)
    assert (checkout.path / "pinned" / "catalog.md").read_text() == "patterns\n"
    assert git(checkout.path, "diff", "--name-only", "HEAD") == ""


def test_retiring_a_checkout_with_submodules_keeps_held_work_and_drops_scratch(repository,
                                                                                tmp_path):
    held = Checkout.add(repository, tmp_path / "unit--P1", "HEAD", "--detach")
    (held.path / "owner.py").write_text("edited\n")
    with pytest.raises(CheckoutHoldsWork):
        held.retire()
    assert held.path.is_dir()
    scratch = ScratchCheckout.add(repository, tmp_path / "check", "HEAD", "--detach")
    (scratch.path / "owner.py").write_text("left by the check\n")
    scratch.retire()
    clean = Checkout.add(repository, tmp_path / "unit--P2", "HEAD", "--detach")
    clean.retire()
    assert not scratch.path.exists() and not clean.path.exists()
    assert {each.path for each in Checkout.of_repository(repository)} == {
        repository.resolve(), held.path.resolve()}
