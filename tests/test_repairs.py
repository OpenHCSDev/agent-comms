import subprocess
import sys
from pathlib import Path

from repairs import ImportCycle, ImportHoist

PACKAGE = {
    "__init__.py": "",
    "leaf.py": "VALUE = 1\n",
    "loop.py": "from .user import used\n\nLOOPED = used\n",
    "user.py": (
        "def used():\n"
        "    from .leaf import VALUE\n"
        "\n"
        "    return VALUE\n"
        "\n"
        "\n"
        "def looped():\n"
        "    from .loop import LOOPED\n"
        "\n"
        "    return LOOPED\n"
    ),
}


def tree(root: Path) -> Path:
    for name, source in PACKAGE.items():
        path = root / "src" / "agent_comms" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    return root


def test_a_local_import_moves_up_unless_a_load_time_import_leads_back(tmp_path: Path) -> None:
    repo = tree(tmp_path)
    hoist = ImportHoist(repo, ["src/agent_comms/user.py"])
    diff, changed = hoist.simulation()
    assert changed == 1
    assert "+from .leaf import VALUE" in diff
    assert "-    from .leaf import VALUE" in diff
    assert "-    from .loop import LOOPED" not in diff
    assert hoist.left == {ImportCycle: 1}

    assert ImportHoist(repo, ["src/agent_comms/user.py"]).apply() == ("src/agent_comms/user.py",)
    source = (repo / "src/agent_comms/user.py").read_text()
    assert source.startswith("from .leaf import VALUE\n")
    assert "    from .loop import LOOPED\n" in source
    loaded = subprocess.run(
        [sys.executable, "-c", "import agent_comms.loop, agent_comms.user"],
        cwd=repo,
        env={"PYTHONPATH": str(repo / "src")},
        capture_output=True,
        text=True,
    )
    assert loaded.returncode == 0, loaded.stderr
