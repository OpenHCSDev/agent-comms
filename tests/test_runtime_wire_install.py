"""The install compares source contracts against the outgoing child's stamped revision."""

import ast
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
from fixture_git import IDENTITY
from module_layers import OutgoingWire

from agent_comms.bus import Bus
from agent_comms.cli_bridges.monitor import PauseSession
from agent_comms.cli_commands import InstallCliCommand
from agent_comms.field_codec import FieldCodec
from agent_comms.installer.commands import InstallCommands
from agent_comms.installer.launch import InstallRun
from agent_comms.installer.upgrade import TargetTree
from agent_comms.maintenance_barrier import MaintenanceBarrier
from agent_comms.installer.upgrade import Stack
from agent_comms.launch_runtime import LaunchRuntime


@pytest.fixture
def outgoing_tree(tmp_path):
    wire = OutgoingWire.at()
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    for entry in wire.imports:
        path = checkout / entry.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text((wire.checkout / entry.path).read_text())
    cutover = checkout / "tools/cutover"
    cutover.mkdir(parents=True)
    (cutover / "__init__.py").touch()
    subprocess.run(("git", "init", "-q", str(checkout)), check=True)
    subprocess.run(("git", "-C", str(checkout), "add", "src", "tools"), check=True)
    subprocess.run(("git", *IDENTITY, "-C", str(checkout),
                    "commit", "-qm", "outgoing source"), check=True)
    return wire, checkout


def stamped_child(tmp_path, checkout):
    """Controlled foreign process response; production reads this value from CoreStamp.

    No old library is imported into the target process, and the selected commit need
    not be either HEAD or its parent.
    """
    commit = subprocess.check_output(("git", "-C", str(checkout), "rev-parse", "HEAD"), text=True).strip()
    child = tmp_path / "runtime" / "bin" / "python"
    child.parent.mkdir(parents=True)
    child.write_text(f"#!/bin/sh\nprintf '%s\\n' {shlex.quote(commit)}\n")
    child.chmod(0o700)
    return LaunchRuntime(child)


def test_crosses_runtimes_installed_stamp_refuses_session_drift_before_maintenance(outgoing_tree, tmp_path):
    wire, checkout = outgoing_tree
    session, = (entry for entry in wire.imports if entry.names == (PauseSession.__name__,))
    path = checkout / session.path
    tree = ast.parse(path.read_text())
    declaration, = (node for node in tree.body
                   if isinstance(node, ast.ClassDef) and node.name == PauseSession.__name__)
    field, = (node for node in declaration.body if isinstance(node, ast.AnnAssign))
    field.target = ast.Name(id="old_process", ctx=ast.Store())
    path.write_text(ast.unparse(tree))
    subprocess.run(("git", *IDENTITY, "-C", str(checkout),
                    "commit", "-qam", "old wire"), check=True)
    runtime = stamped_child(tmp_path, checkout)
    # HEAD is now compatible. Only the installed stamp still selects the older fields.
    path.write_text((wire.checkout / session.path).read_text())
    subprocess.run(("git", *IDENTITY, "-C", str(checkout),
                    "commit", "-qam", "new compatible HEAD"), check=True)
    home = tmp_path / "home"
    pointer = home / ".local/share/agent-comms/current"
    pointer.parent.mkdir(parents=True)
    pointer.symlink_to(runtime.prefix, target_is_directory=True)
    root = tmp_path / "bus"
    root.mkdir()
    Bus.install(Bus.path_in(root))
    barrier = MaintenanceBarrier(root / "registry.json")
    target = subprocess.check_output(("git", "-C", str(checkout), "rev-parse", "HEAD"), text=True).strip()
    scratch = tmp_path / "attempt"
    scratch.mkdir()
    target_tree = TargetTree(checkout, scratch, target)
    target_tree.add()
    request = InstallRun(InstallCliCommand(checkout=checkout, spill_dir=tmp_path), root, scratch, target)
    result = subprocess.run(
        (sys.executable, "-m", "agent_comms.installer"),
        input=json.dumps(FieldCodec.encode(request)), text=True, capture_output=True,
        env={**os.environ, "HOME": str(home),
             "PYTHONPATH": os.pathsep.join(map(str, (wire.checkout / "src", target_tree.steps_package)))},
        timeout=60,
    )
    assert result.returncode == 1, result.stderr
    assert "PauseSession declares (old_process: ProcessIdentity), sent as (process: ProcessIdentity)" in result.stderr
    assert not any(path.exists() for path in barrier.witness_paths)
    assert not scratch.exists()


def test_crosses_runtimes_accepts_matching_installed_source(outgoing_tree, tmp_path):
    _, checkout = outgoing_tree
    with InstallCommands() as commands:
        Stack(checkout, commands, runtime=stamped_child(tmp_path, checkout)).require_runtime_wire()
    assert not tuple(tmp_path.glob("runtime-wire-*"))
