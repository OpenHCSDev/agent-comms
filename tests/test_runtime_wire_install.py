"""The install compares source contracts against the outgoing child's stamped revision."""

import ast
import shlex
import subprocess

import pytest
from module_layers import OutgoingWire

import fixture_git
from agent_comms.cli_bridges.monitor import PauseSession
from agent_comms.execution.launch_runtime import LaunchRuntime
from agent_comms.installer.commands import InstallCommands
from agent_comms.installer.refusals import RuntimeWireChanged
from agent_comms.installer.upgrade import Stack


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
    fixture_git.run(checkout, "commit", "-qm", "outgoing source")
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


def test_crosses_runtimes_installed_stamp_refuses_session_drift(outgoing_tree, tmp_path):
    """The installed stamp, not HEAD, selects the outgoing wire: a commit whose PauseSession
    field differs is refused by the check the install runs before maintenance
    (upgrade.py: installed.require_runtime_wire())."""
    wire, checkout = outgoing_tree
    session, = (entry for entry in wire.imports if entry.names == (PauseSession.__name__,))
    path = checkout / session.path
    tree = ast.parse(path.read_text())
    declaration, = (node for node in tree.body
                   if isinstance(node, ast.ClassDef) and node.name == PauseSession.__name__)
    field, = (node for node in declaration.body if isinstance(node, ast.AnnAssign))
    field.target = ast.Name(id="old_process", ctx=ast.Store())
    path.write_text(ast.unparse(tree))
    fixture_git.run(checkout, "commit", "-qam", "old wire")
    runtime = stamped_child(tmp_path, checkout)
    # HEAD is now compatible. Only the installed stamp still selects the older fields.
    path.write_text((wire.checkout / session.path).read_text())
    fixture_git.run(checkout, "commit", "-qam", "new compatible HEAD")
    with InstallCommands() as commands, pytest.raises(RuntimeWireChanged) as refused:
        Stack(checkout, commands, runtime=runtime).require_runtime_wire()
    assert "PauseSession declares (old_process: ProcessIdentity), sent as (process: ProcessIdentity)" in str(refused.value)


def test_crosses_runtimes_accepts_matching_installed_source(outgoing_tree, tmp_path):
    _, checkout = outgoing_tree
    with InstallCommands() as commands:
        Stack(checkout, commands, runtime=stamped_child(tmp_path, checkout)).require_runtime_wire()
    assert not tuple(tmp_path.glob("runtime-wire-*"))
