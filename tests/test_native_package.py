"""Provider-free complete-package provenance and filesystem-shape controls."""

import hashlib
import json
import os
import runpy
import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_comms import native_package
from agent_comms.native_package import (
    NativePackageError,
    package_tree_digest,
    share_native_resources,
    verify_native_package,
)

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX native package filesystem")


@pytest.fixture
def package(tmp_path, monkeypatch):
    root = tmp_path / "package"
    dependency = root / "node_modules/dependency"
    dependency.mkdir(parents=True)
    (dependency / "index.js").write_text("export const value = 1;\n")
    (dependency / "package.json").write_text('{"main":"index.js"}')
    (root / "package.json").write_text('{"name":"fixture"}')
    manifest = tmp_path / "pinned.sha256"
    manifest.write_text(native_package.TREE_PREFIX + package_tree_digest(root) + "\n")
    monkeypatch.setattr(native_package, "MANIFEST", manifest)
    return root


def test_complete_package_is_reproducible_across_copy_and_mtime(package, tmp_path):
    copy = tmp_path / "copy"
    shutil.copytree(package, copy)
    os.utime(copy / "package.json", (1, 1))
    verify_native_package(package)
    verify_native_package(copy)
    assert package_tree_digest(package) == package_tree_digest(copy)


@pytest.mark.parametrize(
    "change", ["dependency", "metadata", "hidden", "extra-directory", "remove"]
)
def test_complete_tree_rejects_unpinned_changes(package, change):
    if change == "dependency":
        (package / "node_modules/dependency/index.js").write_text("export const value = 2;\n")
    elif change == "metadata":
        (package / "node_modules/dependency/package.json").write_text('{"main":"evil.js"}')
    elif change == "hidden":
        (package / ".hidden-loader.js").write_text("changed")
    elif change == "extra-directory":
        (package / "extra").mkdir()
    else:
        (package / "package.json").unlink()
    with pytest.raises(NativePackageError, match="differs from pinned"):
        verify_native_package(package)


@pytest.mark.parametrize(
    "shape", ["symlink-file", "symlink-directory", "hardlink", "fifo", "writable"]
)
def test_unsafe_filesystem_shape_fails_without_opening_special_files(package, shape):
    extra = package / "extra"
    if shape == "symlink-file":
        extra.symlink_to(package / "package.json")
    elif shape == "symlink-directory":
        extra.symlink_to(package / "node_modules", target_is_directory=True)
    elif shape == "hardlink":
        os.link(package / "package.json", extra)
    elif shape == "fifo":
        os.mkfifo(extra)
    else:
        (package / "package.json").chmod(0o666)
    with pytest.raises(NativePackageError, match="symlinks or special|owner-controlled|shared resource is writable"):
        verify_native_package(package)


def test_new_build_shares_only_equal_frozen_resources(package, tmp_path):
    deployments = tmp_path / "deployments"
    donor = deployments / ".pi-native-first/node_modules/@earendil-works/pi-coding-agent"
    fresh = tmp_path / "new-build"
    for destination in (donor, fresh):
        shutil.copytree(package, destination)
        (destination / "dist").mkdir()
        (destination / "dist/agent-comms-import-fence.mjs").write_bytes(
            (Path(__file__).resolve().parents[1] / "stack/native-import-fence.mjs").read_bytes()
        )
    share_native_resources(donor, deployments)
    original_digest = package_tree_digest(donor)
    original_file = donor / "node_modules/dependency/index.js"
    changed_file = fresh / "node_modules/dependency/index.js"
    changed_file.write_text("export const value = 2;\n")
    before = package_tree_digest(fresh)
    result = share_native_resources(fresh, deployments)
    assert result["shared_files"] > 0 and result["shared_content_bytes"] > 0
    assert os.path.samefile(donor / "package.json", fresh / "package.json")
    assert not os.path.samefile(original_file, changed_file)
    assert original_file.read_text() == "export const value = 1;\n"
    assert package_tree_digest(donor) == original_digest
    assert package_tree_digest(fresh) == before
    assert not (fresh / "package.json").stat().st_mode & 0o222


def test_previous_import_fence_is_never_a_resource_donor(package, tmp_path):
    deployments = tmp_path / "deployments"
    donor = deployments / ".pi-native-old/node_modules/@earendil-works/pi-coding-agent"
    fresh = tmp_path / "new-build"
    for destination, fence in ((donor, "old independent-file fence"), (fresh, "new immutable fence")):
        shutil.copytree(package, destination)
        (destination / "dist").mkdir()
        (destination / "dist/agent-comms-import-fence.mjs").write_text(fence)
    for path in donor.rglob("*"):
        if path.is_file():
            path.chmod(path.stat().st_mode & ~0o222)
    original_digest = package_tree_digest(donor)
    assert share_native_resources(fresh, deployments)["shared_files"] == 0
    assert (donor / "package.json").stat().st_nlink == 1
    assert package_tree_digest(donor) == original_digest


def test_readonly_resource_borrow_does_not_change_content_provenance(package, tmp_path, monkeypatch):
    resource = package / "package.json"
    resource.chmod(0o444)
    read = os.read
    borrowed = False

    def acquire_name(fd, count):
        nonlocal borrowed
        block = read(fd, count)
        if block and not borrowed:
            borrowed = True
            os.link(resource, tmp_path / "another-deployment-resource")
        return block

    monkeypatch.setattr(os, "read", acquire_name)
    verify_native_package(package)
    assert borrowed


@pytest.mark.parametrize("limit", ["MAX_BYTES", "MAX_ENTRIES", "MAX_DEPTH"])
def test_inventory_work_is_bounded(package, monkeypatch, limit):
    monkeypatch.setattr(native_package, limit, 1)
    with pytest.raises(NativePackageError, match="limit exceeded"):
        verify_native_package(package)


def test_oversized_directory_is_refused_before_materializing_listing(package, monkeypatch):
    produced = 0

    def names():
        nonlocal produced
        while True:
            produced += 1
            assert produced <= 2, "unbounded directory enumeration"
            yield SimpleNamespace(name=str(produced))

    @contextmanager
    def scan(path):
        yield names()

    monkeypatch.setattr(native_package, "MAX_ENTRIES", 2)
    monkeypatch.setattr(os, "scandir", scan)
    with pytest.raises(NativePackageError, match="inventory limit"):
        verify_native_package(package)
    assert produced == 2


def test_changed_file_during_read_is_rejected(package, monkeypatch):
    read = os.read
    fired = False

    def changed(fd, count):
        nonlocal fired
        block = read(fd, count)
        if block and not fired:
            fired = True
            (package / "node_modules/dependency/index.js").write_text("changed during read\n")
        return block

    monkeypatch.setattr(os, "read", changed)
    with pytest.raises(NativePackageError, match="changed while hashing"):
        verify_native_package(package)


@pytest.mark.parametrize("pin", ["", "# agent-comms-native-tree-v1 invalid\n"])
def test_missing_or_malformed_commitment_is_not_a_success_marker(package, pin):
    native_package.MANIFEST.write_text(pin)
    with pytest.raises(NativePackageError, match="commitment unavailable"):
        verify_native_package(package)


def test_failed_tree_verification_precedes_journal_creation(package, tmp_path, monkeypatch):
    from agent_comms import native_compaction_writer as writer
    from agent_comms import owner_compaction_commit as commit

    manager = package / "dist/core/session-manager.js"
    manager.parent.mkdir(parents=True)
    manager.write_text("// matching manager alone is insufficient\n")
    native_package.MANIFEST.write_text(
        native_package.TREE_PREFIX + package_tree_digest(package) + "\n"
    )
    (package / "node_modules/dependency/index.js").write_text("// drift outside manager\n")
    monkeypatch.setattr(writer.shutil, "which", lambda executable: f"/fixture/{executable}")
    with pytest.raises(NativePackageError, match="differs from pinned"):
        commit.OwnerCompactionCommit(tmp_path / "registry.json", package)
    assert not (tmp_path / "compaction-commits.sqlite3").exists()


def test_copied_helper_must_match_packaged_resource_before_journal(package, tmp_path, monkeypatch):
    from agent_comms import native_compaction_writer as writer
    from agent_comms import owner_compaction_commit as commit

    helper = package / "dist/agent-comms-compaction-commit-child.mjs"
    helper.parent.mkdir()
    helper.write_text("// inconsistent SDK/helper release")
    native_package.MANIFEST.write_text(
        native_package.TREE_PREFIX + package_tree_digest(package) + "\n"
    )
    monkeypatch.setattr(writer.shutil, "which", lambda executable: f"/fixture/{executable}")
    with pytest.raises(ValueError, match="differs from packaged resource"):
        commit.OwnerCompactionCommit(tmp_path / "registry.json", package)
    assert not (tmp_path / "compaction-commits.sqlite3").exists()


@pytest.mark.parametrize("include_resources", [False, True])
def test_installed_resource_lookup_never_guesses_adjacent_stack(tmp_path, include_resources):
    installed = tmp_path / "site-packages/agent_comms"
    installed.mkdir(parents=True)
    module = installed / "native_package.py"
    shutil.copy2(native_package.__file__, module)
    if not include_resources:
        with pytest.raises(ValueError, match="Installed native package resources"):
            runpy.run_path(str(module))
        return
    resources = installed / "_native"
    resources.mkdir()
    (resources / "pi-native.sha256").write_text("packaged pin")
    (resources / "native-compaction-commit-child.mjs").write_text("// packaged helper")
    namespace = runpy.run_path(str(module))
    assert namespace["MANIFEST"] == resources / "pi-native.sha256"
    assert namespace["COMPACTION_HELPER"] == resources / "native-compaction-commit-child.mjs"


@pytest.fixture
def launcher(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    stack = tmp_path / "stack"
    (stack / "bin").mkdir(parents=True)
    source = tmp_path / "src/agent_comms"
    source.mkdir(parents=True)
    shutil.copy2(repo / "src/agent_comms/native_package.py", source)
    for name in ("pi-native", "prepare-pi-native"):
        shutil.copy2(repo / "stack/bin" / name, stack / "bin" / name)
    staged = tmp_path / "staged"
    (staged / "dist").mkdir(parents=True)
    (staged / "dist/cli.js").write_text(
        "console.log(JSON.stringify([process.env.COPIED_BOOTSTRAP,"
        "process.env.NODE_OPTIONS,process.env.NODE_PATH]));\n"
    )
    (staged / "dist/agent-comms-project-bootstrap.mjs").write_text(
        "process.env.COPIED_BOOTSTRAP = 'trusted';\n"
    )
    shutil.copyfile(
        repo / "stack/native-import-fence.mjs", staged / "dist/agent-comms-import-fence.mjs"
    )
    (staged / "dist/agent-comms-imports.json").write_text(
        json.dumps({"version": 2, "extensionEntries": [], "peerAliases": {}})
    )
    (staged / "dependency.js").write_text("// full tree coverage, not in short manifest\n")
    manifest = (
        hashlib.sha256((staged / "dist/cli.js").read_bytes()).hexdigest()
        + "  dist/cli.js\n"
        + native_package.TREE_PREFIX
        + package_tree_digest(staged)
        + "\n"
    )
    (stack / "pi-native.sha256").write_text(manifest)
    build = hashlib.sha256(manifest.encode()).hexdigest()[:16]
    package = stack / f".pi-native-{build}/node_modules/@earendil-works/pi-coding-agent"
    package.parent.mkdir(parents=True)
    staged.rename(package)
    return stack, package


@pytest.mark.skipif(not shutil.which("node"), reason="Local Node fixture only")
def test_launcher_uses_only_verified_copied_bootstrap(launcher, tmp_path):
    stack, _ = launcher
    marker = tmp_path / "untrusted-ran"
    preload = tmp_path / "untrusted.mjs"
    preload.write_text(
        "import {writeFileSync} from 'node:fs';"
        f"writeFileSync({json.dumps(str(marker))}, 'bad');process.exit(99);"
    )
    env = dict(os.environ, NODE_OPTIONS=f"--import={preload.as_uri()}", NODE_PATH="untrusted")
    result = subprocess.run(
        [str(stack / "bin/pi-native")], env=env, capture_output=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ["trusted", None, None]
    assert not marker.exists()


@pytest.mark.parametrize("script", ["pi-native", "prepare-pi-native"])
def test_launch_and_existing_prepare_refuse_unlisted_dependency_drift(launcher, script):
    stack, package = launcher
    (package / "dependency.js").write_text("// unpinned dependency change\n")
    result = subprocess.run([str(stack / "bin" / script)], capture_output=True, timeout=10)
    assert result.returncode != 0
    assert not result.stdout
    assert b"differs from pinned artifact" in result.stderr
    assert (package / "dependency.js").read_text() == "// unpinned dependency change\n"
