"""A2 has one implementation owner, including inherited and renamed references."""

import ast
import importlib.util
from pathlib import Path

import pytest

pytestmark = pytest.mark.refactor_guard
OWNER = "agent_comms.field_codec"


def codec_subclasses(sources: dict[str, str]) -> set[str]:
    """Resolve local bindings and module imports until inheritance reaches a fixed point."""
    bindings: dict[str, str] = {}
    classes: dict[str, list[str]] = {}

    def reference(node: ast.AST, scope: str) -> str:
        if isinstance(node, ast.Name):
            return f"{scope}.{node.id}"
        if isinstance(node, ast.Attribute):
            return f"{reference(node.value, scope)}.{node.attr}"
        if isinstance(node, ast.Subscript):
            return reference(node.value, scope)
        return ""

    def scan(nodes: list[ast.stmt], scope: str, module: str) -> None:
        for node in nodes:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    bindings[f"{scope}.{alias.asname or alias.name.split('.')[0]}"] = (
                        alias.name if alias.asname else alias.name.split(".")[0]
                    )
            elif isinstance(node, ast.ImportFrom):
                package = module.split(".")[: -node.level] if node.level else []
                source = ".".join([*package, *([node.module] if node.module else [])])
                for alias in node.names:
                    if alias.name == "*":
                        # Star imports include a possible inherited codec owner.
                        for name in exports.get(source, ()):
                            bindings[f"{scope}.{name}"] = f"{source}.{name}"
                    else:
                        bindings[f"{scope}.{alias.asname or alias.name}"] = f"{source}.{alias.name}"
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name) and node.value is not None:
                        value = reference(node.value, scope)
                        if value:
                            bindings[f"{scope}.{target.id}"] = value
            elif isinstance(node, ast.ClassDef):
                name = f"{scope}.{node.name}"
                classes[name] = [reference(base, scope) for base in node.bases]
                scan(node.body, name, module)
            else:
                # Includes conditional imports and declarations inside functions.
                for _, value in ast.iter_fields(node):
                    if isinstance(value, list) and value and isinstance(value[0], ast.stmt):
                        scan(value, scope, module)

    trees = {module: ast.parse(source) for module, source in sources.items()}
    exports = {
        module: {node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)}
        | {
            alias.asname or alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        }
        for module, tree in trees.items()
    }
    for module, tree in trees.items():
        scan(tree.body, module, module)

    def resolve(name: str) -> str:
        seen = set()
        while name not in seen:
            seen.add(name)
            parts = name.split(".")
            for size in range(len(parts), 0, -1):
                prefix = ".".join(parts[:size])
                if prefix in bindings:
                    name = ".".join([bindings[prefix], *parts[size:]])
                    break
            else:
                return name
        return name

    family = {f"{OWNER}.FieldCodec"}
    while True:
        expanded = family | {
            name
            for name, bases in classes.items()
            if any(resolve(base) in family for base in bases)
        }
        if expanded == family:
            return {name for name in family if not name.startswith(f"{OWNER}.")}
        family = expanded


def test_no_field_codec_subclasses_outside_its_owner():
    root = Path(__file__).resolve().parents[2] / "src"
    sources = {
        ".".join(path.relative_to(root).with_suffix("").parts).removesuffix(
            ".__init__"
        ): path.read_text()
        for path in root.rglob("*.py")
    }
    companion = importlib.util.find_spec("toad")
    if companion is not None:
        for location in companion.submodule_search_locations or ():
            root = Path(location)
            sources.update({
                "toad." + ".".join(path.relative_to(root).with_suffix("").parts).removesuffix(
                    ".__init__"
                ): path.read_text()
                for path in root.rglob("*.py")
            })
    assert codec_subclasses(sources) == set()


def test_codec_owner_rejects_subclasses_even_through_an_alias():
    from agent_comms.field_codec import FieldCodec

    alias = FieldCodec
    with pytest.raises(TypeError, match="sealed mechanism FieldCodec"):
        class ExternalCodec(alias):
            pass


def test_guard_follows_aliases_qualified_imports_and_inherited_reexports():
    sources = {
        OWNER: "class FieldCodec: pass\nclass Shared(FieldCodec): pass",
        "agent_comms.a": (
            "from .field_codec import FieldCodec as F\n"
            "Alias = F\nclass Bad(Alias): pass"
        ),
        "agent_comms.b": "import agent_comms.field_codec as fc\nclass Bad(fc.FieldCodec): pass",
        "agent_comms.c": "from .a import Bad as Imported\nclass Worse(Imported): pass",
        "agent_comms.d": (
            "import agent_comms.field_codec\n"
            "class Bad(agent_comms.field_codec.Shared): pass"
        ),
        "agent_comms.e": (
            "from agent_comms import field_codec as fc\n"
            "class Bad(fc.FieldCodec): pass"
        ),
        "agent_comms.f": "from .field_codec import *\nclass Bad(Shared): pass",
        "agent_comms.safe": "class FieldCodec: pass\nclass Fine(FieldCodec): pass",
    }
    assert codec_subclasses(sources) == {
        f"agent_comms.{module}.{name}"
        for module, name in [
            ("a", "Bad"),
            ("b", "Bad"),
            ("c", "Worse"),
            ("d", "Bad"),
            ("e", "Bad"),
            ("f", "Bad"),
        ]
    }
