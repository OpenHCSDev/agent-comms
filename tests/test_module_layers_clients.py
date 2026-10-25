"""A stack client's imports root reachability and fail on modules Core lacks."""

import ast
import os
import shutil
import subprocess
import sys
from pathlib import Path

from module_layers import OutgoingWire, Package, StackClient


def write(path: Path, text: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_client_imports_root_reachability_and_name_missing_modules(tmp_path):
    core = tmp_path / "core"
    write(
        core / "pyproject.toml",
        '[project.scripts]\nac = "agent_comms.cli:main"\nguard = "agent_comms_hooks.guard:main"\n',
    )
    write(core / "src/agent_comms/__init__.py")
    write(core / "src/agent_comms/cli.py")
    write(core / "src/agent_comms/ingress.py")
    write(core / "src/agent_comms/orphan.py")
    write(core / "src/agent_comms/rules.py")
    write(core / "src/agent_comms_hooks/guard.py", "from agent_comms_hooks import shared\n")
    write(core / "src/agent_comms_hooks/shared.py", "from agent_comms.rules import Rule\n")
    client = tmp_path / "toad"
    write(
        client / "src/toad/session.py",
        "from agent_comms.ingress import Ingress\nimport agent_comms.deleted\n"
        "import os\nimport agent_comms_hooks.watch\nimport agent_comms\n",
    )

    package = Package(core, (StackClient("toad", client),))

    assert [str(entry) for entry in package.missing] == [
        "toad:src/toad/session.py:2 imports agent_comms.deleted"
    ]
    assert package.client_surface == {"agent_comms.ingress": {"toad"}}
    assert package.unreached == ("agent_comms.orphan",)


def test_client_names_must_be_defined_by_their_module(tmp_path):
    core = tmp_path / "core"
    write(core / "pyproject.toml", '[project.scripts]\nac = "agent_comms.cli:main"\n')
    write(core / "src/agent_comms/__init__.py")
    write(core / "src/agent_comms/cli.py")
    write(
        core / "src/agent_comms/routes.py",
        "from os import environ as env\n"
        "class Route: ...\n"
        "if True:\n    def current(): ...\n"
        "DEFAULT, OTHER = 1, 2\n",
    )
    client = tmp_path / "toad"
    write(
        client / "tests/test_routes.py",
        "from agent_comms.routes import Route, current, env, OTHER, implicit\n"
        "from agent_comms.routes import *\n"
        "from agent_comms import routes\n",
    )

    package = Package(core, (StackClient("toad", client),))

    assert [(str(entry), name) for entry, name in package.missing_names] == [
        ("toad:tests/test_routes.py:1 imports agent_comms.routes", "implicit")
    ]


def wire_base(tmp_path: Path, wire: OutgoingWire) -> Path:
    """A base tree holding only the modules the outgoing wire names, copied from this tree."""
    base = tmp_path / "base"
    write(base / "pyproject.toml", '[project.scripts]\nac = "agent_comms.cli:main"\n')
    write(base / "src/agent_comms/__init__.py")
    for entry in wire.imports:
        write(base / entry.path, (wire.checkout / entry.path).read_text())
    return base


def test_crosses_runtimes_matches_the_tree_that_declares_it(tmp_path: Path) -> None:
    wire = OutgoingWire.at()

    package = Package(wire_base(tmp_path, wire), (wire,))

    assert (package.missing, package.missing_names, package.drifted) == ((), (), ())


def test_crosses_runtimes_fails_on_a_renamed_session_field(tmp_path: Path) -> None:
    from agent_comms.cli_bridges.monitor import PauseSession

    wire = OutgoingWire.at()
    base = wire_base(tmp_path, wire)
    session, = (entry for entry in wire.imports if entry.names == (PauseSession.__name__,))
    path = base / session.path
    tree = ast.parse(path.read_text())
    declaration, = (node for node in tree.body
                   if isinstance(node, ast.ClassDef) and node.name == PauseSession.__name__)
    field, = (node for node in declaration.body if isinstance(node, ast.AnnAssign))
    field.target = ast.Name(id="renamed_process", ctx=ast.Store())
    path.write_text(ast.unparse(tree))

    package = Package(base, (wire,))

    assert (package.missing, package.missing_names) == ((), ())
    assert [drift for _, drift in package.drifted] == [
        "PauseSession declares (renamed_process: ProcessIdentity), sent as (process: ProcessIdentity)"
    ]


def test_crosses_runtimes_missing_file_and_class_share_one_verdict(tmp_path: Path) -> None:
    from agent_comms.cli_bridges.monitor import PauseSession

    wire = OutgoingWire.at()
    base = wire_base(tmp_path, wire)
    session, = (entry for entry in wire.imports if entry.names == (PauseSession.__name__,))
    path = base / session.path
    tree = ast.parse(path.read_text())
    tree.body = [node for node in tree.body
                 if not (isinstance(node, ast.ClassDef) and node.name == PauseSession.__name__)]
    path.write_text(ast.unparse(tree))
    missing_class = tuple(problem for entry, problem in Package(base, (wire,)).drifted
                          if entry.names == session.names)
    path.unlink()
    missing_file = tuple(problem for entry, problem in Package(base, (wire,)).drifted
                         if entry.names == session.names)

    assert missing_file == missing_class == (
        "PauseSession is missing; sent fields (process: ProcessIdentity)",
    )


def test_crosses_runtimes_discovers_sessions_without_importing_monitor(tmp_path: Path) -> None:
    subprocess.run(
        (sys.executable, "-c",
         "import sys; from agent_comms.installer.runtime_wire import CrossesRuntimes; "
         "assert 'agent_comms.cli_bridges.monitor' not in sys.modules; "
         "assert any(entry.names == ('PauseSession',) for entry in CrossesRuntimes.declarations())"),
        check=True, timeout=60,
        env={**os.environ, "PYTHONPATH": str(OutgoingWire.at().checkout / "src")},
    )


def test_crosses_runtimes_discovers_a_new_session_member(tmp_path: Path) -> None:
    source = tmp_path / "src"
    shutil.copytree(OutgoingWire.at().checkout / "src/agent_comms", source / "agent_comms",
                    ignore=shutil.ignore_patterns("__pycache__", ".git"))
    script = source / "agent_comms/new_session.py"
    write(script, '''
from dataclasses import dataclass
from agent_comms.cli_bridges.monitor import SessionRequest, SessionReady

@dataclass(frozen=True)
class FreshSession(SessionRequest[SessionReady], declared_name="fresh"):
    fresh_field: str

    async def handle(self, service, peer):
        return SessionReady()
''')
    subprocess.run(
        (sys.executable, "-c",
         "from agent_comms.installer.runtime_wire import CrossesRuntimes; "
         "entry, = (entry for entry in CrossesRuntimes.declarations() if entry.names == ('FreshSession',)); "
         "assert entry.fields == ('fresh_field: str',)"), check=True,
        env={**os.environ, "PYTHONPATH": str(source)},
        timeout=60,
    )
