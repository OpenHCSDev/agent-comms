"""Build qualification and runtime acquisition have different trust boundaries."""

from pathlib import Path

import pytest

import agent_comms.native_package as native_package
from agent_comms.private_path import PrivateDirectoryRole
from native_package_acquisition_fixture import PackageTrace
from test_native_package import launcher as launcher
from test_native_package import load_resource_module


def test_manifest_build_hashes_content_once(launcher):
    stack, package = launcher
    namespace = load_resource_module(stack.parent / "src/agent_comms/native_package.py")
    trace = PackageTrace(namespace["package_tree_digest"].__code__)
    with trace.active():
        namespace["write_native_manifest"](package)
    assert trace.tree_hash_calls == 1


def test_materialized_directories_do_not_copy_checkout_permissions(tmp_path):
    source = tmp_path / "source"
    nested = source / "nested"
    nested.mkdir(parents=True)
    (nested / "entry.mjs").write_text("export const value = 1;\n")
    prepare = load_resource_module(
        Path(__file__).resolve().parents[1] / "stack/prepare-native-import-boundary.py")
    digests = set()
    for mode in (0o700, 0o755):
        source.chmod(mode)
        nested.chmod(mode)
        destination = tmp_path / f"copy-{mode:o}"
        prepare["materialize"](source, destination)
        for directory in (destination, *destination.rglob("*/")):
            assert directory.stat().st_mode & 0o777 == PrivateDirectoryRole.permissions
        digests.add(native_package.package_tree_digest(destination))
    assert len(digests) == 1


def test_qualification_refuses_changed_content(launcher):
    stack, package = launcher
    namespace = load_resource_module(stack.parent / "src/agent_comms/native_package.py")
    owner = namespace["VerifiedNativePackage"]
    assert owner.qualify(package).path == package
    (package / "dependency.js").write_text("// changed after pinning\n")
    with pytest.raises(namespace["NativePackageError"], match="differs from pinned artifact"):
        owner.qualify(package)


def test_acquisition_refuses_package_outside_this_codes_native_resources(launcher):
    _, package = launcher
    with pytest.raises(native_package.NativePackageError,
                       match="outside this code's native resources"):
        native_package.VerifiedNativePackage.acquire(package)


def test_prepared_native_fixture_is_qualified(native_runtime_package):
    assert native_runtime_package.path == native_package.pinned_location()


@pytest.mark.asyncio
async def test_private_bus_agent_construction_does_not_acquire_native_package(private_comms):
    from agent_comms.acp import CommsAgent

    trace = PackageTrace(native_package.package_tree_digest.__code__)
    with trace.active():
        agent = CommsAgent(private_comms, auto_wake=False)
        try:
            assert agent.sessions.config.comms is private_comms
        finally:
            await agent.shutdown()
    assert trace.tree_hash_calls == 0


def test_acquisition_reads_no_content(native_runtime_package):
    """Acquiring this code's own qualified package hashes nothing and reads no content."""
    trace = PackageTrace(native_package.package_tree_digest.__code__)
    with trace.active():
        package = native_package.VerifiedNativePackage.acquire(native_runtime_package.path)
    assert package.path == native_runtime_package.path
    assert trace.tree_hash_calls == 0
    assert trace.content_reads == 0
