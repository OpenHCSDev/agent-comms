"""A stack client's imports root reachability and fail on modules Core lacks."""

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
        write(base / "src" / entry.path, (wire.checkout / entry.path).read_text())
    return base


def test_outgoing_wire_matches_the_tree_that_declares_it(tmp_path: Path) -> None:
    wire = OutgoingWire.at()

    package = Package(wire_base(tmp_path, wire), (wire,))

    assert (package.missing, package.missing_names, package.drifted) == ((), (), ())


def test_outgoing_wire_fails_on_a_renamed_field_or_a_missing_probe(tmp_path: Path) -> None:
    wire = OutgoingWire.at()
    base = wire_base(tmp_path, wire)
    owners = base / "src/agent_comms/installer/probes/owners.py"
    owners.write_text(owners.read_text().replace(
        "class RunningOwnersProbe(RuntimeProbe[list[str]]):\n    root:",
        "class RunningOwnersProbe(RuntimeProbe[list[str]]):\n    bus_root:",
    ))
    publication = base / "src/agent_comms/installer/probes/publication.py"
    publication.write_text(publication.read_text().replace("class PublicationProbe", "class Gone"))

    package = Package(base, (wire,))

    assert [name for _, name in package.missing_names] == ["PublicationProbe"]
    assert [drift for _, drift in package.drifted] == [
        "RunningOwnersProbe declares (bus_root: Annotated[Path, PathText]), "
        "sent as (root: Annotated[Path, PathText])"
    ]
