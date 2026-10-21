"""The layer each agent_comms module belongs to, derived from its name and its imports.

The plan's table (plans/remaining-slop-plan.md, Order 1) names each layer's module
prefixes; a module no prefix claims takes the highest layer among the modules it
imports, so it adds no violation of its own. Every import that still runs downward
in the order, or into a top layer from outside it, is a violation.

The stack (stack/pyproject.toml) declares the client packages installed with Core from
git sources: Toad and Textual import agent_comms modules. Each client is read at its
branch head, so its imports root the reachability like a console script does. Modules
neither reaches are listed as deletion candidates, and a client import naming a module
Core no longer has fails --check, the step the merge queue runs.

    python tools/module_layers.py > docs/layout/MODULE_LAYERS.md
    python tools/module_layers.py --check
"""

from __future__ import annotations

import argparse
import ast
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = "agent_comms"


@dataclass(frozen=True)
class Layer:
    """A package of the target structure and the module name prefixes it owns."""

    name: str
    prefixes: tuple[str, ...]
    top: bool = False

    def claims(self, module: str) -> bool:
        head = module.split(".")[1]
        return head == self.name or any(head.startswith(prefix) for prefix in self.prefixes)


LAYERS = (
    Layer("wire", ("bus_", "wire_", "message_", "delivery_", "private_")),
    Layer("registry", ("registry_", "registration_", "owner_", "thread_", "channel_")),
    Layer("coordination", ("goal_", "attempt_", "coordination_", "wake_")),
    Layer("context", ("working_", "context_segments", "instructions")),
    Layer("turns", ("turn_", "input_", "session_", "selected_")),
    Layer(
        "runtime", ("native_", "pi_", "acp_", "agent_", "runtime_", "_pi_helpers", "cli_bridges")
    ),
    Layer("ui_model", (), top=True),
    Layer("cli", ("cli_", "tool_", "tools"), top=True),
    Layer("commander", (), top=True),
)


@dataclass(frozen=True)
class Import:
    """One import statement inside agent_comms naming another agent_comms module."""

    source: str
    target: str
    line: int


class ImportNames(ast.NodeVisitor):
    """Records each agent_comms module one source file names in an import statement
    or a literal import_module call."""

    def __init__(self, package: Package, module: str, path: Path, found: list[Import]) -> None:
        self.package, self.module, self.found = package, module, found
        # relative imports start at the file's own directory, a package's __init__ included
        self.parent = ".".join(path.parent.relative_to(package.src).parts)

    def record(self, node: ast.stmt | ast.expr, *names: str) -> None:
        for name in names:
            if (target := self.package.resolve(name)) is not None:
                self.found.append(Import(self.module, target, node.lineno))

    def visit_Import(self, node: ast.Import) -> None:
        self.record(node, *(alias.name for alias in node.names))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        parts = self.parent.split(".")
        base = ".".join(parts[: len(parts) - node.level + 1]) if node.level else ""
        stem = ".".join(part for part in (base, node.module or "") if part)
        self.record(node, *(f"{stem}.{alias.name}" for alias in node.names))

    def visit_Call(self, node: ast.Call) -> None:
        match node:
            case ast.Call(
                func=ast.Attribute(attr="import_module"), args=[ast.Constant(str() as name), *_]
            ):
                self.record(node, name)
        self.generic_visit(node)


@dataclass(frozen=True)
class ClientImport:
    """One import statement in a stack client naming an agent_comms module."""

    client: str
    path: str
    line: int
    module: str
    names: tuple[str, ...] = ()

    def __str__(self) -> str:
        return f"{self.client}:{self.path}:{self.line} imports {self.module}"


class ClientImportNames(ast.NodeVisitor):
    """Records each absolute agent_comms module one client file imports."""

    def __init__(self, client: str, path: str, found: list[ClientImport]) -> None:
        self.client, self.path, self.found = client, path, found

    def record(self, node: ast.stmt | ast.expr, module: str, names: tuple[str, ...] = ()) -> None:
        if module.split(".")[0] == PACKAGE:
            self.found.append(ClientImport(self.client, self.path, node.lineno, module, names))

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.record(node, alias.name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        match node:
            case ast.ImportFrom(module=str(module), level=0):
                self.record(node, module, tuple(alias.name for alias in node.names))

    def visit_Call(self, node: ast.Call) -> None:
        match node:
            case ast.Call(
                func=ast.Attribute(attr="import_module"), args=[ast.Constant(str() as name), *_]
            ):
                self.record(node, name)
        self.generic_visit(node)


class TopLevelNames(ast.NodeVisitor):
    """The names one module binds at its top level: definitions, assignments and imports,
    including those inside top-level if and try blocks."""

    def __init__(self) -> None:
        self.names: set[str] = set()

    @classmethod
    def of(cls, tree: ast.Module) -> frozenset[str]:
        visitor = cls()
        for statement in tree.body:
            visitor.visit(statement)
        return frozenset(visitor.names)

    def bind(self, target: ast.expr) -> None:
        match target:
            case ast.Name(id=name):
                self.names.add(name)
            case ast.Tuple(elts=elements) | ast.List(elts=elements):
                for element in elements:
                    self.bind(element)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.names.add(node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.names.add(node.name)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.names.add(node.name)

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            self.bind(target)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self.bind(node.target)

    def visit_TypeAlias(self, node: ast.TypeAlias) -> None:
        self.bind(node.name)

    def visit_Import(self, node: ast.Import) -> None:
        self.names.update((alias.asname or alias.name).split(".")[0] for alias in node.names)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self.names.update(alias.asname or alias.name for alias in node.names)


@dataclass(frozen=True)
class StackClient:
    """A package the stack installs from a git source, read from a checkout of it."""

    name: str
    checkout: Path

    @staticmethod
    def sources(stack: Path) -> dict[str, tuple[str, str]]:
        """Each git source stack/pyproject.toml declares: name to (url, branch)."""
        match tomllib.loads((stack / "pyproject.toml").read_text()):
            case {"tool": {"uv": {"sources": dict() as sources}}}:
                found = {}
                for name, source in sources.items():
                    match source:
                        case {"git": str(url), "branch": str(branch)}:
                            found[name] = (url, branch)
                return found
        raise ValueError(f"{stack / 'pyproject.toml'} declares no [tool.uv.sources] table")

    @classmethod
    def fetched(cls, stack: Path, into: Path) -> tuple[StackClient, ...]:
        """Shallow clones of every git source at its declared branch head."""
        clients = []
        for name, (url, branch) in StackClient.sources(stack).items():
            checkout = into / name
            subprocess.run(
                ["git", "clone", "--quiet", "--depth", "1", "--branch", branch, url, str(checkout)],
                check=True,
            )
            clients.append(cls(name, checkout))
        return tuple(clients)

    @cached_property
    def imports(self) -> tuple[ClientImport, ...]:
        found: list[ClientImport] = []
        for path in sorted(self.checkout.rglob("*.py")):
            relative = str(path.relative_to(self.checkout))
            ClientImportNames(self.name, relative, found).visit(
                ast.parse(path.read_text(), relative)
            )
        return tuple(found)


class Package:
    """The agent_comms source tree: its modules, their imports and their layers."""

    def __init__(self, root: Path, clients: tuple[StackClient, ...] = ()) -> None:
        self.src = root / "src"
        self.pyproject = root / "pyproject.toml"
        self.root = self.src / PACKAGE / "__init__.py"  # read for reachability, never a unit
        self.clients = clients

    @cached_property
    def files(self) -> dict[str, Path]:
        return {
            ".".join(path.relative_to(self.src).with_suffix("").parts).removesuffix(
                ".__init__"
            ): path
            for path in sorted(set((self.src / PACKAGE).rglob("*.py")) - {self.root})
        }

    @cached_property
    def top_level(self) -> tuple[str, ...]:
        """Each top-level module and subpackage: the units the layer map assigns."""
        return tuple(sorted({".".join(name.split(".")[:2]) for name in self.files}))

    @cached_property
    def subpackages(self) -> tuple[str, ...]:
        return tuple(
            unit for unit in self.top_level if self.src.joinpath(*unit.split(".")).is_dir()
        )

    def unit(self, module: str) -> str:
        return ".".join(module.split(".")[:2])

    def resolve(self, name: str) -> str | None:
        """The module an import names, or None when it is outside agent_comms."""
        while name and name not in self.files:
            name = name.rpartition(".")[0]
        return name or None

    @cached_property
    def imports(self) -> tuple[Import, ...]:
        found: list[Import] = []
        for module, path in {PACKAGE: self.root, **self.files}.items():
            ImportNames(self, module, path, found).visit(ast.parse(path.read_text(), str(path)))
        return tuple(sorted(set(found), key=lambda edge: (edge.source, edge.line, edge.target)))

    @cached_property
    def layers(self) -> dict[str, Layer]:
        """Each unit's layer: its claiming prefix, else the highest layer it imports."""
        claimed = {
            unit: next(layer for layer in LAYERS if layer.claims(unit))
            for unit in self.top_level
            if any(layer.claims(unit) for layer in LAYERS)
        }
        rank = {unit: LAYERS.index(layer) for unit, layer in claimed.items()}
        rank |= {unit: 0 for unit in self.top_level if unit not in claimed}
        changed = True
        while changed:
            changed = False
            for edge in self.unit_imports:
                source, target = self.unit(edge.source), self.unit(edge.target)
                if source not in claimed and source != target and rank[target] > rank[source]:
                    rank[source], changed = rank[target], True
        return {unit: LAYERS[rank[unit]] for unit in self.top_level}

    @cached_property
    def unit_imports(self) -> tuple[Import, ...]:
        """Imports between units; the package root is read for reachability only."""
        return tuple(edge for edge in self.imports if edge.source in self.files)

    @cached_property
    def violations(self) -> tuple[Import, ...]:
        """Imports into a later layer, or into a top layer from outside it."""
        order = {layer: index for index, layer in enumerate(LAYERS)}
        found = []
        for edge in self.unit_imports:
            source, target = (
                self.layers[self.unit(edge.source)],
                self.layers[self.unit(edge.target)],
            )
            if source is not target and (target.top or order[target] > order[source]):
                found.append(edge)
        return tuple(found)

    @cached_property
    def entry_points(self) -> tuple[str, ...]:
        match tomllib.loads(self.pyproject.read_text()):
            case {"project": {"scripts": dict() as scripts}}:
                return tuple(sorted({target.partition(":")[0] for target in scripts.values()}))
        raise ValueError(f"{self.pyproject} declares no [project.scripts] table")

    def provides(self, module: str) -> bool:
        return module == PACKAGE or module in self.files

    def client_targets(self, entry: ClientImport) -> tuple[str, ...]:
        """The modules one client import loads: a named submodule, else the module itself."""
        if not self.provides(entry.module):
            return ()
        named = tuple(f"{entry.module}.{name}" for name in entry.names)
        return tuple(name for name in named if name in self.files) or (entry.module,)

    @cached_property
    def client_imports(self) -> tuple[ClientImport, ...]:
        return tuple(entry for client in self.clients for entry in client.imports)

    @cached_property
    def missing(self) -> tuple[ClientImport, ...]:
        """Client imports naming an agent_comms module this tree does not have."""
        return tuple(entry for entry in self.client_imports if not self.provides(entry.module))

    def defined_names(self, module: str) -> frozenset[str]:
        """Names a module binds at its top level, re-exports included; a module defining
        __getattr__ answers every name."""
        path = self.root if module == PACKAGE else self.files[module]
        return TopLevelNames.of(ast.parse(path.read_text(), str(path)))

    @cached_property
    def missing_names(self) -> tuple[tuple[ClientImport, str], ...]:
        """Names a client imports from an existing module that neither defines them nor
        has them as submodules."""
        found = []
        for entry in self.client_imports:
            if not self.provides(entry.module):
                continue
            defined = self.defined_names(entry.module)
            if "__getattr__" in defined:
                continue
            found += [
                (entry, name)
                for name in entry.names
                if name != "*"
                and name not in defined
                and f"{entry.module}.{name}" not in self.files
            ]
        return tuple(found)

    @cached_property
    def client_surface(self) -> dict[str, set[str]]:
        """Each unit a client imports directly, with the clients importing it."""
        surface: dict[str, set[str]] = {}
        for entry in self.client_imports:
            for target in self.client_targets(entry):
                if target != PACKAGE:
                    surface.setdefault(self.unit(target), set()).add(entry.client)
        return dict(sorted(surface.items()))

    @cached_property
    def unreached(self) -> tuple[str, ...]:
        """Units no console script or stack client reaches through static imports,
        package __init__s included."""
        edges: dict[str, set[str]] = {}
        for edge in self.imports:
            edges.setdefault(edge.source, set()).add(edge.target)
        clients = (target for entry in self.client_imports for target in self.client_targets(entry))
        seen, todo = set(), [PACKAGE, *self.entry_points, *clients]
        while todo:
            module = todo.pop()
            if module in seen:
                continue
            seen.add(module)
            parts = module.split(".")
            todo.extend(".".join(parts[:end]) for end in range(1, len(parts)))
            todo.extend(edges.get(module, ()))
        return tuple(unit for unit in self.top_level if not any(self.unit(m) == unit for m in seen))

    def markdown(self) -> str:
        lines = [
            "# Module layers",
            "",
            "Generated by `python tools/module_layers.py > docs/layout/MODULE_LAYERS.md`;"
            " do not edit by hand.",
            "",
            "Layers in order: " + ", ".join(f"`{layer.name}`" for layer in LAYERS) + ". "
            "Each layer imports only the layers before it; "
            "`ui_model`, `cli` and `commander` sit on top, "
            "and nothing outside a top layer imports it. A module no prefix in "
            "`plans/remaining-slop-plan.md` (Order 1) claims takes the highest layer it imports.",
            "",
            f"{len(self.top_level)} top-level units ({len(self.subpackages)} of them subpackages); "
            f"{len(self.violations)} violating imports; {len(self.unreached)} deletion candidates.",
            "",
        ]
        for layer in LAYERS:
            units = [unit for unit in self.top_level if self.layers[unit] is layer]
            lines += [f"## {layer.name} ({len(units)})", ""]
            lines += [f"- `{unit.removeprefix(PACKAGE + '.')}`" for unit in units]
            lines.append("")
        lines += [
            "## Violating imports",
            "",
            "| File:line | Imports | Layer | Into layer |",
            "|---|---|---|---|",
        ]
        for edge in self.violations:
            path = self.files[edge.source].relative_to(self.src.parent)
            source, target = (
                self.layers[self.unit(edge.source)],
                self.layers[self.unit(edge.target)],
            )
            lines.append(
                f"| `{path}:{edge.line}` | `{edge.target}` | {source.name} | {target.name} |"
            )
        lines += [
            "",
            "## Stack client imports",
            "",
            "Units the git sources in `stack/pyproject.toml` import directly, read at each "
            "branch head. Deleting one breaks that client; `--check` refuses it.",
            "",
            "| Unit | Imported by |",
            "|---|---|",
        ]
        lines += [
            f"| `{unit.removeprefix(PACKAGE + '.')}` | {', '.join(sorted(clients))} |"
            for unit, clients in self.client_surface.items()
        ]
        lines += [
            "",
            "## Deletion candidates",
            "",
            "No console script in `pyproject.toml` and no stack client reaches these through "
            "static imports or literal `import_module` calls. Prove each unused with an import "
            "hook before deleting it.",
            "",
        ]
        lines += [f"- `{unit.removeprefix(PACKAGE + '.')}`" for unit in self.unreached]
        return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.partition("\n")[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail when a stack client imports an agent_comms module this tree lacks",
    )
    arguments = parser.parse_args()
    with tempfile.TemporaryDirectory() as clones:
        package = Package(ROOT, StackClient.fetched(ROOT / "stack", Path(clones)))
        if not arguments.check:
            sys.stdout.write(package.markdown())
            return 0
        for entry in package.missing:
            print(f"missing module: {entry}", file=sys.stderr)
        for entry, name in package.missing_names:
            print(f"missing name: {entry} {name}", file=sys.stderr)
        print(
            f"{len(package.client_imports)} client imports of {len(package.client_surface)} units; "
            f"{len(package.missing)} name a module this tree lacks, "
            f"{len(package.missing_names)} a name its module does not define"
        )
        return 1 if package.missing or package.missing_names else 0


if __name__ == "__main__":
    sys.exit(main())
