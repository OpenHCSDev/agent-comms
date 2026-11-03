"""A real remote, launch checkout and private staged bundle exercise publication authority."""

from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.checkout_publication import PublishingCheckout
from agent_comms.cli_verbs.publish_checkout import PublishCheckoutCliCommand
from agent_comms.cli_verbs.sync_checkout import SyncCheckoutCliCommand
from agent_comms.repo import Checkout, GitCommandError
from agent_comms.tool_denial import StorageDenied
from agent_comms.storage_projection import StorageProjection
from agent_comms.storage_rules import StorageRule
from agent_comms.threads import Thread
from agent_comms.tool_caller import ThreadCaller
from source_revision_fixture import SourceRepository


def test_canonical_sync_and_publication_preserve_source_and_prior_release(private_comms, tmp_path):
    comms = private_comms
    comms.registry.storage_rule = StorageRule(
        volume=tmp_path, floor_gib=0, checkout_base=tmp_path / "checkouts",
        scratch_base=tmp_path / "scratch",
    )
    repository = SourceRepository.create(tmp_path / "git", baseline_files={Path(".gitignore"): "release/\ncurrent\nliteral*\n"})
    checkout = PublishingCheckout(repository.checkout)
    user_file = checkout.path / "user-note"
    user_file.write_text("keep me")
    user_file.chmod(0o640)
    old = checkout.path / "release/old"
    old.mkdir(parents=True)
    (old / "report").write_text("previous")
    (checkout.path / "current").symlink_to("release/old")
    worker = comms.registry.declare(Thread("publisher", frozenset(), Checkout(checkout.path)))
    caller = ThreadCaller(worker.name)
    rule = comms.registry.storage_rule
    with pytest.raises(StorageDenied):
        StorageProjection(rule, worker, comms).require_write(checkout.path / "owner.py")
    # A real upstream descendant is pulled, rather than locally manufacturing a ref.
    repository.git(checkout.path, "switch", "feature")
    (checkout.path / "literal[0]").symlink_to("release/old")
    repository.git(checkout.path, "add", "--force", "--", "literal[0]")
    repository.git(checkout.path, "commit", "-m", "Track a literal symlink before ignoring its namespace")
    repository.git(checkout.path, "switch", "main")
    repository.git(checkout.path, "push", "origin", "feature:main")
    synchronized = SyncCheckoutCliCommand(caller=caller).apply(comms)
    assert synchronized.head == repository.git(checkout.path, "rev-parse", "origin/main").strip()
    source = rule.roots_of(worker).scratch / "staged"
    source.mkdir(parents=True)
    (source / "report").write_text("accepted report")
    (source / "report").chmod(0o640)
    command = PublishCheckoutCliCommand(caller=caller, source=source,
        destination=Path("release/new"), link=Path("current"), previous=Path("release/old"),
        head=synchronized.head)
    published = command.apply(comms)
    assert published.files == 1
    assert (checkout.path / "current/report").read_text() == "accepted report"
    assert (checkout.path / "current/report").stat().st_mode == (source / "report").stat().st_mode
    assert (old / "report").read_text() == "previous"
    assert user_file.read_text() == "keep me" and user_file.stat().st_mode & 0o777 == 0o640
    with pytest.raises(FileExistsError):
        command.apply(comms)
    with pytest.raises(ValueError, match="link changed"):
        replace(command, destination=Path("release/another")).apply(comms)
    assert not (checkout.path / "release/another").exists()
    for destination in (Path("owner.py"), Path("user-note"), Path("../escape"), Path(".git/objects/new")):
        with pytest.raises(ValueError):
            replace(command, destination=destination, previous=Path("release/new")).apply(comms)
    with pytest.raises(ValueError, match="Stage"):
        replace(command, source=old).apply(comms)
    with pytest.raises(ValueError, match="tracked source"):
        replace(command, destination=Path("release/literal"), link=Path("literal[0]")).apply(comms)
    assert (checkout.path / "literal[0]").readlink() == Path("release/old")
    # A linked source cannot turn a read-only native/scientific original into a bundle.
    (source / "external").symlink_to(user_file)
    with pytest.raises(ValueError, match="regular files"):
        replace(command, destination=Path("release/linked"), previous=Path("release/new")).apply(comms)
    assert not (checkout.path / "release/linked").exists()
    # Dirty and locally-ahead source are both refused, not reset or declared synchronized.
    (checkout.path / "owner.py").write_text("local work\n")
    with pytest.raises(ValueError, match="tracked changes"):
        SyncCheckoutCliCommand(caller=caller).apply(comms)
    repository.commit_change("Unpublished local work", "owner.py")
    with pytest.raises(GitCommandError):
        SyncCheckoutCliCommand(caller=caller).apply(comms)
    with pytest.raises(StorageDenied):
        StorageProjection(rule, worker, comms).require_write(checkout.path / "owner.py")
