"""Build qualification and runtime acquisition have different trust boundaries."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import agent_comms.native_package as native_package
from agent_comms.field_codec import FieldCodec
from agent_comms.launch_runtime import LaunchRuntime
from native_package_acquisition_fixture import AcquisitionSample, PackageTrace
from test_native_package import launcher as launcher
from test_native_package import load_resource_module


def test_manifest_build_hashes_content_once(launcher):
    stack, package = launcher
    namespace = load_resource_module(stack.parent / "src/agent_comms/native_package.py")
    trace = PackageTrace(namespace["package_tree_digest"].__code__)
    previous = sys.getprofile()
    sys.setprofile(trace)
    try:
        namespace["write_native_manifest"](package)
    finally:
        sys.setprofile(previous)
    assert trace.tree_hash_calls == 1


def test_qualification_refuses_changed_content(launcher):
    stack, package = launcher
    namespace = load_resource_module(stack.parent / "src/agent_comms/native_package.py")
    owner = namespace["VerifiedNativePackage"]
    assert owner.qualify(package).path == package
    (package / "dependency.js").write_text("// changed after pinning\n")
    with pytest.raises(namespace["NativePackageError"], match="differs from pinned artifact"):
        owner.qualify(package)


def test_acquisition_refuses_package_outside_running_runtime(launcher):
    _, package = launcher
    with pytest.raises(native_package.NativePackageError, match="outside the running runtime prefix"):
        native_package.VerifiedNativePackage.acquire(package)


def test_actual_runtime_acquisition_reads_no_content():
    runtime = LaunchRuntime.installed()
    fixture = Path(__file__).with_name("native_package_acquisition_fixture.py")
    result = subprocess.run(
        (str(runtime.interpreter), str(fixture), native_package.__file__),
        env=runtime.environment(os.environ),
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    sample = FieldCodec.decode(AcquisitionSample, json.loads(result.stdout))
    assert sample.path.is_relative_to(runtime.prefix)
    assert sample.tree_hash_calls == 0
    assert sample.content_reads == 0
    print(f"Installed-prefix acquisition: {sample.seconds:.6f}s; no hashing or content reads")
