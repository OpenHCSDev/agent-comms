"""Provider-free complete-package provenance and filesystem-shape controls."""

import hashlib
import importlib.util
import sys
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

import agent_comms.native_package as native_package
from agent_comms.core_stamp import CheckoutStamp
from agent_comms.native_build_inputs import INPUTS_PREFIX, NativeBuildInputs
from agent_comms.native_package import (
    package_tree_digest,
)

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX native package filesystem")


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


@pytest.fixture
def launcher(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    stack = tmp_path / "stack"
    (stack / "bin").mkdir(parents=True)
    source = tmp_path / "src/agent_comms"
    source.mkdir(parents=True)
    shutil.copytree(repo / 'src/agent_comms', source, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns('__pycache__'))
    CheckoutStamp(str(repo)).write(source / 'package_stamp.py')
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
    package = native_package.pinned_package_for(stack / "pi-native.sha256")
    package.parent.mkdir(parents=True)
    staged.rename(package)
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
    prepared = native_package.pinned_package_for(stack / "pi-native.sha256").parents[2]
    package.parents[2].rename(prepared)
    deployments = tmp_path / "deployments"
    launch = [str(stack / "bin/prepare-pi-native"), "--publish", str(deployments)]

    # Keep this publication fixture independent of the ambient Node installation.
    interpreter = tmp_path / "interpreter"
    interpreter.mkdir()
    (interpreter / "node").write_text("#!/bin/sh\nexit 0\n")
    (interpreter / "node").chmod(0o755)
    env = dict(os.environ, PATH=f"{interpreter}:{os.environ['PATH']}")

    def run():
        result = subprocess.run(launch, capture_output=True, text=True, timeout=15, env=env)
        assert result.returncode == 0, result.stderr

    run()
    assert (deployments / f"native-current-{build}").is_dir()
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


def test_publication_refuses_unlisted_dependency_drift(launcher, tmp_path):
    stack, package = launcher
    (package / "dependency.js").write_text("// unpinned dependency change\n")
    deployments = tmp_path / "deployments"
    result = subprocess.run(
        [str(stack / "bin/prepare-pi-native"), "--publish", str(deployments)],
        capture_output=True, timeout=300,
    )
    assert deployments.is_dir()
    assert not any(deployments.iterdir())
    assert result.returncode != 0
    assert not result.stdout
    assert b"differs from pinned artifact" in result.stderr
    assert (package / "dependency.js").read_text() == "// unpinned dependency change\n"
