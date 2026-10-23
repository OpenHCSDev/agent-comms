"""Provider-free complete-package provenance and filesystem-shape controls."""

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import agent_comms.native_package as native_package
from agent_comms.core_stamp import WheelStamp
from agent_comms.native_build_inputs import INPUTS_PREFIX, NativeBuildInputs
from agent_comms.native_failures import NativePiUnavailable
from agent_comms.native_package import (
    NativePackageError,
    NativePackageStore,
    package_tree_digest,
)
from agent_comms.package_stamp import STAMP

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX native package filesystem")


def test_unbuilt_pin_is_native_pi_unavailable_and_names_its_preparation(tmp_path, monkeypatch):
    manifest = tmp_path / "stack/pi-native.sha256"
    manifest.parent.mkdir()
    manifest.write_text(f"{'0' * 64}  dist/cli.js\n")
    monkeypatch.setattr(native_package, "MANIFEST", manifest)
    with pytest.raises(NativePiUnavailable, match="run .*/stack/bin/prepare-pi-native first"):
        native_package.pinned_native_package()


def test_pinned_inputs_refuse_changed_patch(tmp_path):
    stack = tmp_path / "stack"
    stack.mkdir()
    patch = stack / "patch-native-example.py"
    patch.write_bytes(b"first patch byte\n")
    manifest = stack / "pi-native.sha256"
    stock = "# agent-comms-native-stock-v1 fixture@1\n"
    manifest.write_text(stock)
    inputs = NativeBuildInputs(tmp_path)
    manifest.write_text(stock + INPUTS_PREFIX + inputs.digest(manifest) + "\n")
    inputs.require_pinned(manifest)
    patch.write_bytes(b"First patch byte\n")
    with pytest.raises(ValueError):
        inputs.require_pinned(manifest)


def test_sealed_tree_is_published_as_a_store_link(tmp_path):
    store = NativePackageStore.at_home(tmp_path / "home")
    manifest = tmp_path / "stack/pi-native.sha256"
    manifest.parent.mkdir()
    manifest.write_text("pinned artifact\n")
    sealed = store.package_for(manifest)
    (sealed / "dist").mkdir(parents=True, mode=0o700)
    resource = sealed / "dist/cli.js"
    resource.write_text("console.log(1)\n")
    resource.chmod(0o500)
    (sealed / "dist").chmod(0o500)
    sealed.chmod(0o500)
    published = tmp_path / "published"
    store.link(manifest, published)
    linked = published / sealed.parents[2].name
    assert linked.is_symlink()
    assert linked.resolve() == sealed.parents[2]
    assert (linked / native_package.PACKAGE_LAYOUT / "dist/cli.js").stat().st_ino == resource.stat().st_ino
    shutil.rmtree(published)
    assert resource.read_text() == "console.log(1)\n"
    store.retire(sealed.parents[2])
    assert not sealed.exists()


def load_resource_module(path):
    spec = importlib.util.spec_from_file_location('agent_comms.resource_fixture', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        return vars(module)
    finally:
        sys.modules.pop(spec.name)


@pytest.mark.parametrize("include_resources", [False, True])
def test_installed_resource_lookup_never_guesses_adjacent_stack(tmp_path, include_resources):
    installed = tmp_path / "site-packages/agent_comms"
    installed.mkdir(parents=True)
    module = installed / "native_package.py"
    shutil.copy2(native_package.__file__, module)
    if not include_resources:
        with pytest.raises(ValueError):
            load_resource_module(module)
        return
    resources = installed / "_native"
    resources.mkdir()
    (resources / "pi-native.sha256").write_text("packaged pin")
    namespace = load_resource_module(module)
    assert namespace["MANIFEST"] == resources / "pi-native.sha256"


def test_checkout_resources_remain_owned_by_stack_when_build_artifacts_exist(tmp_path):
    source = tmp_path / "src/agent_comms"
    source.mkdir(parents=True)
    module = source / "native_package.py"
    shutil.copy2(native_package.__file__, module)
    artifacts = source / "_native"
    artifacts.mkdir()
    (artifacts / "core_stamp.py").write_text("# unrelated build metadata\n")
    namespace = load_resource_module(module)
    assert namespace["MANIFEST"] == tmp_path / "stack/pi-native.sha256"
    assert namespace["OWNER_INSTRUCTIONS"] == tmp_path / "stack/APPEND_SYSTEM.md"
    assert namespace["UNIT_INSTRUCTIONS"] == tmp_path / "stack/UNIT_SYSTEM.md"


def stamp_prepared(stack, env):
    manifest = stack / "pi-native.sha256"
    stamp = NativeBuildInputs(stack.parent).preparation_digest(
        manifest,
        subprocess.check_output(["node", "--version"], env=env, text=True).strip(),
        subprocess.check_output(["npm", "--version"], env=env, text=True).strip(),
        env.get("PI_MCP_DEPENDENCY_PACKAGE", ""),
    )
    prepared = NativePackageStore.at_home(Path.home()).package_for(manifest).parents[2]
    namespace = load_resource_module(stack.parent / "src/agent_comms/native_package.py")
    namespace["VerifiedNativePackage"].prepare(prepared / native_package.PACKAGE_LAYOUT)
    (prepared / ".prepared-inputs.sha256").write_text(stamp + "\n")


@pytest.fixture
def launcher(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = Path(__file__).resolve().parents[1]
    stack = tmp_path / "stack"
    (stack / "bin").mkdir(parents=True)
    source = tmp_path / "src/agent_comms"
    source.mkdir(parents=True)
    shutil.copytree(repo / 'src/agent_comms', source, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns('__pycache__'))
    WheelStamp(STAMP.commit()).write(source / 'package_stamp.py')
    # The environment `uv sync --project stack` installs: this checkout's agent_comms, editable,
    # with its dependencies.
    interpreter = stack / ".venv/bin/python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text(f'#!/bin/sh\nexec {sys.executable} "$@"\n')
    interpreter.chmod(0o755)
    for name in ("pi-native", "prepare-pi-native", "native-source"):
        shutil.copy2(repo / "stack/bin" / name, stack / "bin" / name)
    shutil.copyfile(repo / "stack/APPEND_SYSTEM.md", stack / "APPEND_SYSTEM.md")
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
    store = NativePackageStore.at_home(Path.home())
    package = store.package_for(stack / "pi-native.sha256")
    package.parent.mkdir(parents=True)
    staged.rename(package)
    store.link(stack / "pi-native.sha256", stack)
    stamp_prepared(stack, dict(os.environ))
    return stack, package


@pytest.mark.skipif(not shutil.which("node"), reason="Local Node fixture only")
def test_qualified_build_uses_only_verified_copied_bootstrap(launcher, tmp_path):
    stack, root = launcher
    namespace = load_resource_module(stack.parent / "src/agent_comms/native_package.py")
    package = namespace["VerifiedNativePackage"].qualify(root)
    marker = tmp_path / "untrusted-ran"
    preload = tmp_path / "untrusted.mjs"
    preload.write_text(
        "import {writeFileSync} from 'node:fs';"
        f"writeFileSync({json.dumps(str(marker))}, 'bad');process.exit(99);"
    )
    env = dict(os.environ, NODE_OPTIONS=f"--import={preload.as_uri()}", NODE_PATH="untrusted")
    result = subprocess.run(
        package.node("agent-comms-project-bootstrap.mjs"),
        env=package.node_environment(env), capture_output=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ["trusted", None, None]
    assert not marker.exists()


def test_publication_installs_declared_source_with_one_link(launcher, tmp_path):
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
    imports.chmod(0o600)
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
    build = native_package.built_tree_pin(stack / "pi-native.sha256")
    store = NativePackageStore.at_home(Path.home())
    prepared = store.package_for(stack / "pi-native.sha256").parents[2]
    package.parents[2].rename(prepared)
    deployments = tmp_path / "deployments"
    launch = [str(stack / "bin/prepare-pi-native"), "--publish", str(deployments)]

    # Keep this publication fixture independent of the ambient Node installation.
    interpreter = tmp_path / "interpreter"
    interpreter.mkdir()
    (interpreter / "node").write_text("#!/bin/sh\nexit 0\n")
    (interpreter / "node").chmod(0o755)
    env = dict(os.environ, PATH=f"{interpreter}:{os.environ['PATH']}")
    stamp_prepared(stack, env)

    def run():
        result = subprocess.run(launch, capture_output=True, text=True, timeout=15, env=env)
        assert result.returncode == 0, result.stderr

    run()
    assert (deployments / f".pi-native-{build}").is_dir()
    assert destination.read_bytes() == content
    assert destination.stat().st_nlink == 1
    assert stale_link.read_bytes() == b"stale\n"
    installed = destination.stat()
    run()
    assert (destination.stat().st_ino, destination.stat().st_mtime_ns) == (
        installed.st_ino, installed.st_mtime_ns)


def test_prepare_imports_agent_comms_with_the_stack_interpreter_not_path_python3(launcher, tmp_path):
    stack, package = launcher
    stale = [stack / ".pi-native-0123456789abcdef", stack / ".pi-native-fedcba9876543210"]
    store = NativePackageStore.at_home(Path.home())
    for build in stale:
        artifact = store.path / build.name
        (artifact / "node_modules").mkdir(parents=True)
        build.symlink_to(artifact, target_is_directory=True)
    running_stage = stack / ".pi-native.stage.running"
    running_stage.mkdir()
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
    # Dropping old checkout links never deletes artifacts another runtime can still use.
    assert not any(build.exists() for build in stale)
    assert all((store.path / build.name).is_dir() for build in stale)
    assert package.is_dir() and running_stage.is_dir()


def test_publication_refuses_unlisted_dependency_drift(launcher, tmp_path):
    stack, package = launcher
    (package / "dependency.js").chmod(0o600)
    (package / "dependency.js").write_text("// unpinned dependency change\n")
    other = stack / ".pi-native-0123456789abcdef"
    other.mkdir()
    deployments = tmp_path / "deployments"
    result = subprocess.run(
        [str(stack / "bin/prepare-pi-native"), "--publish", str(deployments)],
        capture_output=True, timeout=300,
    )
    assert deployments.is_dir()
    assert not any(deployments.iterdir())
    assert result.returncode != 0
    assert not result.stdout
    assert b"writable" in result.stderr
    assert (package / "dependency.js").read_text() == "// unpinned dependency change\n"
    assert other.is_dir()  # a refused build retires nothing
