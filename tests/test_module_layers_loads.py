"""An import loads its target when its module loads, unless a function call or a type
checker is the only thing that runs it."""

from pathlib import Path

from module_layers import Package

SOURCES = {
    "__init__.py": "",
    "top.py": "",
    "body.py": "",
    "decorator.py": "",
    "default.py": "",
    "checked.py": "",
    "user.py": (
        "from typing import TYPE_CHECKING\n"
        "import importlib\n"
        "from . import top\n"
        "if TYPE_CHECKING:\n"
        "    from . import checked\n"
        "@importlib.import_module('agent_comms.decorator').wrap\n"
        "def f(\n"
        "    x=importlib.import_module('agent_comms.default'),\n"
        "    y=lambda: importlib.import_module('agent_comms.body'),\n"
        "):\n"
        "    from . import body\n"
    ),
}


def test_only_imports_that_run_while_the_module_loads_load_their_target(tmp_path: Path) -> None:
    for name, source in SOURCES.items():
        path = tmp_path / "src" / "agent_comms" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    (tmp_path / "pyproject.toml").write_text('[project.scripts]\nac = "agent_comms.user:main"\n')
    package = Package(tmp_path)
    loads = {
        edge.target: edge.loads for edge in package.imports if edge.source == "agent_comms.user"
    }
    assert loads == {
        "agent_comms.top": True,
        "agent_comms.decorator": True,
        "agent_comms.default": True,
        "agent_comms.checked": False,
        "agent_comms.body": False,
    }
