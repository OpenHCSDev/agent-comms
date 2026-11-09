"""An installation resolves names and physical ownership from one short-lived cut."""

from collections.abc import Callable, Collection
from pathlib import Path

import pytest

from agent_comms.context_store import ContextStore
from agent_comms.errors import RelationViolationError
from agent_comms.registry.threads import ThreadCatalogFile
from agent_comms.storage.locked_store import (
    DeclaredFile,
    InstallationFiles,
    LiveFilesRequest,
    RootFile,
    UndeclaredFile,
)
from agent_comms.storage.store_files import store_lock_path

type Selection = Callable[
    [InstallationFiles, Collection[Path]], tuple[DeclaredFile | UndeclaredFile, ...]
]


@pytest.mark.parametrize("select", (
    InstallationFiles.owned_files,
    InstallationFiles.named_files,
    InstallationFiles.referenced_files,
))
def test_empty_selection_leaves_missing_root_absent(tmp_path: Path, select: Selection) -> None:
    root = tmp_path / "absent"
    assert select(InstallationFiles(LiveFilesRequest(root)), ()) == ()
    assert not root.exists()


def test_named_selection_keeps_primary_and_not_its_companions(tmp_path: Path) -> None:
    primary = ContextStore.path_in(tmp_path)
    primary.touch()
    wal = primary.with_name(primary.name + "-wal")
    wal.touch()
    lock = store_lock_path(primary)
    lock.touch()
    selected = InstallationFiles(LiveFilesRequest(tmp_path)).named_files((
        primary.relative_to(tmp_path), wal.relative_to(tmp_path), lock.relative_to(tmp_path),
    ))
    assert len(selected) == 1
    assert selected[0].live(tmp_path) == primary
    assert selected[0].owner is ContextStore.declaring_owner()


def test_references_preserve_unknown_and_missing_files(tmp_path: Path) -> None:
    present = tmp_path / "unregistered-data"
    present.write_bytes(b"original bytes")
    missing = tmp_path / "missing-data"
    selected = InstallationFiles(LiveFilesRequest(tmp_path)).referenced_files((
        present, missing.relative_to(tmp_path),
    ))
    assert {resource.live(tmp_path) for resource in selected} == {present, missing}
    assert all(resource.owner is RootFile for resource in selected)
    assert present.read_bytes() == b"original bytes"
    assert not missing.exists()


def test_later_observation_sees_a_new_declared_catalog(tmp_path: Path) -> None:
    catalog = ThreadCatalogFile.path_in(tmp_path, lookup="0" * 32)
    name = catalog.relative_to(tmp_path)
    before, = InstallationFiles(LiveFilesRequest(tmp_path)).named_files((name,))
    assert before.owner is RootFile
    catalog.parent.mkdir(parents=True, exist_ok=True)
    catalog.touch()
    after, = InstallationFiles(LiveFilesRequest(tmp_path)).named_files((name,))
    assert after.owner is ThreadCatalogFile.declaring_owner()
    assert after.live(tmp_path) == catalog


def test_selected_symlink_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "original"
    target.write_bytes(b"unchanged")
    alias = tmp_path / "alias"
    alias.symlink_to(target)
    with pytest.raises(RelationViolationError, match="aliased file"):
        InstallationFiles(LiveFilesRequest(tmp_path)).owned_files((alias,))
    assert target.read_bytes() == b"unchanged"


def test_unregistered_external_file_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    external = tmp_path / "outside"
    external.write_bytes(b"unchanged")
    with pytest.raises(RelationViolationError, match="undeclared file"):
        InstallationFiles(LiveFilesRequest(root)).owned_files((external,))
    assert external.read_bytes() == b"unchanged"


@pytest.mark.parametrize("name", (Path("../outside"), Path("/outside")))
def test_escaping_retained_name_is_refused(tmp_path: Path, name: Path) -> None:
    with pytest.raises(RelationViolationError, match="retained name"):
        InstallationFiles(LiveFilesRequest(tmp_path)).named_files((name,))
