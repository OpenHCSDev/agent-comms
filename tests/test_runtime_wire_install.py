"""The install compares source contracts against the outgoing child's stamped revision."""

import ast
import shlex
import subprocess
from pathlib import Path

import pytest
from module_layers import OutgoingWire

from agent_comms.cli_bridges.monitor import PauseSession
from agent_comms.installer.commands import InstallCommands
from agent_comms.installer.refusals import RuntimeWireChanged
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
    subprocess.run(("git", "init", "-q", str(checkout)), check=True)
    subprocess.run(("git", "-C", str(checkout), "config", "user.name", "Wire test"), check=True)
    subprocess.run(("git", "-C", str(checkout), "config", "user.email", "wire@example.invalid"), check=True)
    subprocess.run(("git", "-C", str(checkout), "add", "src"), check=True)
    subprocess.run(("git", "-C", str(checkout), "-c", "user.name=Wire test",
                    "-c", "user.email=wire@example.invalid", "commit", "-qm", "outgoing source"), check=True)
    return wire, checkout


def stamped_child(tmp_path, checkout):
    """Controlled foreign process response; production reads this value from CoreStamp.

    No old library is imported into the target process, and the selected commit need
    not be either HEAD or its parent.
    """
    commit = subprocess.check_output(("git", "-C", str(checkout), "rev-parse", "HEAD"), text=True).strip()
    child = tmp_path / "outgoing-python"
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
    subprocess.run(("git", "-C", str(checkout), "commit", "-qam", "old wire"), check=True)
    runtime = stamped_child(tmp_path, checkout)
    # HEAD is now compatible. Only the installed stamp still selects the older fields.
    path.write_text((wire.checkout / session.path).read_text())
    subprocess.run(("git", "-C", str(checkout), "-c", "user.name=Wire test",
                    "-c", "user.email=wire@example.invalid", "commit", "-qam", "new compatible HEAD"), check=True)
    registry = tmp_path / "bus" / "registry.json"
    with InstallCommands() as commands:
        stack = Stack(checkout, commands, runtime=runtime)
        with pytest.raises(RuntimeWireChanged) as refused:
            stack.require_runtime_wire()
    assert "PauseSession declares (old_process: ProcessIdentity), sent as (process: ProcessIdentity)" in str(refused.value)
    assert not registry.exists()
    assert not tuple(tmp_path.glob("runtime-wire-*"))


def test_crosses_runtimes_accepts_matching_installed_source(outgoing_tree, tmp_path):
    _, checkout = outgoing_tree
    with InstallCommands() as commands:
        Stack(checkout, commands, runtime=stamped_child(tmp_path, checkout)).require_runtime_wire()
    assert not tuple(tmp_path.glob("runtime-wire-*"))
