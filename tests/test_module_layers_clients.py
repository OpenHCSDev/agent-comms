"""A stack client's imports root reachability and fail on modules Core lacks."""

from pathlib import Path

from module_layers import Package, StackClient


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
        "from agent_comms.ingress import Ingress\nimport agent_comms.deleted\n",
    )

    package = Package(core, (StackClient("toad", client),))

    assert [str(entry) for entry in package.missing] == [
        "toad:src/toad/session.py:2 imports agent_comms.deleted"
    ]
    assert package.client_surface == {"agent_comms.ingress": {"toad"}}
    assert package.unreached == ("agent_comms.orphan",)
