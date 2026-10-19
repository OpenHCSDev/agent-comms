"""Provider-free complete-package provenance and filesystem-shape controls."""

import hashlib
import sys
import json
import os
import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

import agent_comms.native_package as native_package
import agent_comms.core_stamp as core_stamp
from agent_comms.core_stamp import CheckoutStamp, WheelStamp
from agent_comms.native_build_inputs import INPUTS_PREFIX, NativeBuildInputs
from agent_comms.native_package import (
    NativePackageError,
    package_tree_digest,
    share_native_resources,
    verify_native_package,
)

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX native package filesystem")


def test_native_environment_refuses_changed_patch(tmp_path, monkeypatch):
    stack = tmp_path / "stack"
    stack.mkdir()
    patch = stack / "patch-native-example.py"
    patch.write_bytes(b"first patch byte\n")
    manifest = stack / "pi-native.sha256"
    stock = "# agent-comms-native-stock-v1 fixture@1\n"
    manifest.write_text(stock)
    inputs = NativeBuildInputs(tmp_path)
    manifest.write_text(stock + INPUTS_PREFIX + inputs.digest(manifest) + "\n")
    monkeypatch.setattr(native_package, "MANIFEST", manifest)
    monkeypatch.setattr(native_package, "_resource_root", lambda: stack)
    assert native_package.native_environment(tmp_path / "cache").parent == tmp_path / "cache"
    patch.write_bytes(b"First patch byte\n")
    with pytest.raises(ValueError, match="inputs changed.*--update-pins"):
        native_package.native_environment(tmp_path / "cache")


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


@pytest.mark.parametrize("stamp, root", [
    (WheelStamp("c0ffee"), Path(core_stamp.__file__).resolve().parent / "_native"),
    (CheckoutStamp("/checkout"), Path("/checkout/stack")),
])
def test_the_package_stamp_alone_decides_where_native_resources_live(stamp, root):
    """A wheel never guesses an adjacent stack; a checkout keeps stack even beside build output."""
    assert stamp.resource("stack", "_native") == root


def test_absent_native_resources_are_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(native_package, "STAMP", CheckoutStamp(str(tmp_path)))
    with pytest.raises(NativePackageError, match="resources are unavailable"):
        native_package._resource_root()


@pytest.fixture
def launcher(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    stack = tmp_path / "stack"
    (stack / "bin").mkdir(parents=True)
    source = tmp_path / "src/agent_comms"
    source.mkdir(parents=True)
    shutil.copytree(repo / 'src/agent_comms', source, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns('__pycache__'))
    # The copy is a checkout of its own: its stamp names it, with a commit to report.
    subprocess.run(("git", "init", "-q", str(tmp_path)), check=True)
    subprocess.run(("git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-q", "--allow-empty", "-m", "fixture"), check=True)
    CheckoutStamp(str(tmp_path)).write(source / 'package_stamp.py')
    # The environment `uv sync --project stack` installs: this checkout's agent_comms, editable,
    # with its dependencies.
    interpreter = stack / ".venv/bin/python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text(f'#!/bin/sh\nexec {sys.executable} "$@"\n')
    interpreter.chmod(0o755)
    for name in ("pi-native", "prepare-pi-native", "native-source"):
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


@pytest.mark.parametrize("command", [["pi-native"], ["prepare-pi-native", "--publish"]])
def test_launch_and_publication_install_declared_source_with_one_link(launcher, tmp_path, command):
    stack, package = launcher
    snapshot = tmp_path / "extensions/global/index.ts"
    snapshot.parent.mkdir(parents=True)
    content = b"export default function declaredSource() {}\n"
    snapshot.write_bytes(content)
    destination = tmp_path / "global/extensions/index.ts"
    destination.parent.mkdir(parents=True)
    stale_link = tmp_path / "stale-extension-source"
    stale_link.write_bytes(b"stale\n")
    os.link(stale_link, destination)

    imports = package / "dist/agent-comms-imports.json"
    imports.write_text(json.dumps({
        "version": 2,
        "extensionEntries": [{
            "entry": "agent-comms-extensions/global/index.mjs",
            "sources": [{
                "path": str(destination),
                "snapshot": str(snapshot.relative_to(tmp_path)),
                "sha256": hashlib.sha256(content).hexdigest(),
                "bytes": len(content),
            }],
        }],
        "peerAliases": {},
    }))
    pin = (
        hashlib.sha256((package / "dist/cli.js").read_bytes()).hexdigest()
        + "  dist/cli.js\n"
        + native_package.TREE_PREFIX
        + package_tree_digest(package)
        + "\n"
    )
    (stack / "pi-native.sha256").write_text(pin)
    build = hashlib.sha256(pin.encode()).hexdigest()[:16]
    prepared = stack / f".pi-native-{build}"
    package.parents[2].rename(prepared)
    deployments = tmp_path / "deployments"
    script, *flags = command
    launch = [str(stack / "bin" / script), *flags, *([str(deployments)] if flags else [])]

    # The launch ends by exec'ing node; the sources are installed before that.
    interpreter = tmp_path / "interpreter"
    interpreter.mkdir()
    (interpreter / "node").write_text("#!/bin/sh\nexit 0\n")
    (interpreter / "node").chmod(0o755)
    env = dict(os.environ, PATH=f"{interpreter}:{os.environ['PATH']}")

    def run():
        result = subprocess.run(launch, capture_output=True, text=True, timeout=15, env=env)
        assert result.returncode == 0, result.stderr

    run()
    assert (deployments / f"native-current-{build}").is_dir() == bool(flags)
    assert destination.read_bytes() == content
    assert destination.stat().st_nlink == 1
    assert stale_link.read_bytes() == b"stale\n"
    installed = destination.stat()
    run()
    assert (destination.stat().st_ino, destination.stat().st_mtime_ns) == (
        installed.st_ino, installed.st_mtime_ns)


def test_prepare_imports_agent_comms_with_the_stack_interpreter_not_path_python3(launcher, tmp_path):
    stack, _ = launcher
    bare = tmp_path / "bare"
    bare.mkdir()
    (bare / "python3").write_text(f'#!/bin/sh\nexec {sys.base_prefix}/bin/python3 -S "$@"\n')
    (bare / "python3").chmod(0o755)
    env = dict(os.environ, PATH=f"{bare}:{os.environ['PATH']}")
    assert subprocess.run(["python3", "-c", "import metaclass_registry"], env=env,
                          capture_output=True).returncode
    result = subprocess.run([str(stack / "bin/prepare-pi-native")], env=env,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"PI_NATIVE_BIN={stack}/bin/pi-native\n"


@pytest.mark.parametrize("script", ["pi-native", "prepare-pi-native"])
def test_launch_and_existing_prepare_refuse_unlisted_dependency_drift(launcher, script):
    stack, package = launcher
    (package / "dependency.js").write_text("// unpinned dependency change\n")
    result = subprocess.run([str(stack / "bin" / script)], capture_output=True, timeout=10)
    assert result.returncode != 0
    assert not result.stdout
    assert b"differs from pinned artifact" in result.stderr
    assert (package / "dependency.js").read_text() == "// unpinned dependency change\n"
