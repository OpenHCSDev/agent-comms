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
    with pytest.raises(NativePackageError, match="links or special|owner-controlled"):
        verify_native_package(package)


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
    from agent_comms import owner_compaction_commit as commit

    manager = package / "dist/core/session-manager.js"
    manager.parent.mkdir(parents=True)
    manager.write_text("// matching manager alone is insufficient\n")
    native_package.MANIFEST.write_text(
        native_package.TREE_PREFIX + package_tree_digest(package) + "\n"
    )
    (package / "node_modules/dependency/index.js").write_text("// drift outside manager\n")
    monkeypatch.setattr(commit, "require_deadline_support", lambda: None)
    monkeypatch.setattr(commit.shutil, "which", lambda executable: f"/fixture/{executable}")
    monkeypatch.setattr(
        commit, "run_authority_child", lambda *a, **k: pytest.fail("native dispatched")
    )
    with pytest.raises(NativePackageError, match="differs from pinned"):
        commit.OwnerCompactionCommit(tmp_path / "registry.json", package)
    assert not (tmp_path / "compaction-commits.sqlite3").exists()


def test_copied_helper_must_match_packaged_resource_before_journal(package, tmp_path, monkeypatch):
    from agent_comms import owner_compaction_commit as commit

    helper = package / "dist/agent-comms-compaction-commit-child.mjs"
    helper.parent.mkdir()
    helper.write_text("// inconsistent SDK/helper release")
    native_package.MANIFEST.write_text(
        native_package.TREE_PREFIX + package_tree_digest(package) + "\n"
    )
    monkeypatch.setattr(commit, "require_deadline_support", lambda: None)
    monkeypatch.setattr(commit.shutil, "which", lambda executable: f"/fixture/{executable}")
    with pytest.raises(ValueError, match="differs from packaged resource"):
        commit.OwnerCompactionCommit(tmp_path / "registry.json", package)
    assert not (tmp_path / "compaction-commits.sqlite3").exists()


@pytest.mark.parametrize("variable", ["NODE_OPTIONS", "NODE_PATH", "NODE_COMPILE_CACHE"])
def test_node_loader_environment_is_not_package_authority(package, tmp_path, monkeypatch, variable):
    from agent_comms import owner_compaction_commit as commit

    monkeypatch.setenv(variable, "untrusted-loader")
    monkeypatch.setattr(commit, "require_deadline_support", lambda: None)
    monkeypatch.setattr(commit.shutil, "which", lambda executable: f"/fixture/{executable}")
    monkeypatch.setattr(commit.OwnerCompactionCommit, "_verify_native", lambda self: None)
    bridge = commit.OwnerCompactionCommit(tmp_path / "registry.json", package)

    def launch(command, *args, **kwargs):
        assert command[:5] == [shutil.which("env"), "-u", "NODE_OPTIONS", "-u", "NODE_PATH"]
        assert command[5:8] == ["-u", "NODE_COMPILE_CACHE", "NODE_DISABLE_COMPILE_CACHE=1"]
        assert command[9:12] == ["--no-global-search-paths", "--import", str(bridge.import_fence)]
        return subprocess.CompletedProcess(command, 1, b'{"status":"unknown","reason":"fixture"}')

    monkeypatch.setattr(commit, "run_authority_child", launch)
    with (tmp_path / "authority").open("w") as fd:
        assert bridge._call(fd.fileno(), {}, 1)["status"] == "unknown"
    assert os.environ[variable] == "untrusted-loader"  # Parent environment not mutated.


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
        json.dumps({"version": 1, "extensionEntries": [], "peerAliases": {}})
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
