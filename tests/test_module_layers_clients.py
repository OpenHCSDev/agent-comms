"""A stack client's imports root reachability and fail on modules Core lacks."""

from pathlib import Path

from module_layers import InstallerZip, Package, StackClient


def write(path: Path, text: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_client_imports_root_reachability_and_name_missing_modules(tmp_path):
    core = tmp_path / "core"
    write(core / "pyproject.toml", '[project.scripts]\nac = "agent_comms.cli:main"\n')
    write(core / "src/agent_comms/__init__.py")
    write(core / "src/agent_comms/cli.py")
    write(core / "src/agent_comms/ingress.py")
    write(core / "src/agent_comms/orphan.py")
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


def test_installer_zip_imports_only_names_the_previous_core_defines(tmp_path):
    previous = tmp_path / "previous"
    write(previous / "pyproject.toml", '[project.scripts]\nac = "agent_comms.cli:main"\n')
    write(previous / "src/agent_comms/__init__.py")
    write(previous / "src/agent_comms/cli.py")
    write(previous / "src/agent_comms/harness_install.py", "class Harness: ...\n")
    current = tmp_path / "current"
    write(
        current / "src/agent_comms/installer/probes/harnesses.py",
        "from typing import TYPE_CHECKING\n"
        "from agent_comms.harness_install import Harness, InstalledHarness\n"
        "from ..publication import Publication\n"
        "if TYPE_CHECKING:\n    from agent_comms.threads import Thread\n",
    )
    write(current / "src/agent_comms/installer/publication.py", "class Publication: ...\n")

    package = Package(previous, (InstallerZip.at(current),))

    assert [(str(entry), name) for entry, name in package.missing_names] == [
        (
            "installer.zip:agent_comms/installer/probes/harnesses.py:2 "
            "imports agent_comms.harness_install",
            "InstalledHarness",
        )
    ]
    assert package.missing == ()
