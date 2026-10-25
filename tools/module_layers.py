"""The layer each agent_comms module belongs to, derived from its name and its imports.

Each layer is a class built on the layers it may import, and names its module
prefixes; a module no prefix claims takes the highest layer among the modules it
imports, so it adds no violation of its own. Every import into a layer the
importing layer is not built on is a violation.

The stack (stack/pyproject.toml) declares the client packages installed with Core from
git sources: Toad and Textual import agent_comms modules. Each client is read at its
branch head, so its imports root the reachability like a console script does. Modules
neither reaches are listed as deletion candidates, and a client import naming a module
Core no longer has fails --check, the step the merge queue runs. That step writes this
report to its job summary, so the report always describes the tree it was run on.

    python tools/module_layers.py
    python tools/module_layers.py --check --report "$GITHUB_STEP_SUMMARY"
"""

from __future__ import annotations

import argparse
import ast
import subprocess
import sys
import tempfile
import tomllib
import typing
from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

from typing_names import TypingImports, TypingReference

from agent_comms.declared_family import DeclaredFamily

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = "agent_comms"


class Layer(DeclaredFamily):
    """A package of the target structure. Its bases are the layers it may import, and
    its prefixes the module names it owns besides its own name."""

    prefixes: typing.ClassVar[tuple[str, ...]] = ()

    @classmethod
    def claim(cls, unit: str) -> int:
        """How much of the unit's name this layer claims; the longest claim owns it."""
        head = unit.split(".")[1]
        claims = [len(prefix) for prefix in cls.prefixes if head.startswith(prefix)]
        if head == cls.declared_name:
            claims.append(len(head))
        return max(claims, default=0)

    @classmethod
    def may_import(cls, target: type[Layer]) -> bool:
        return issubclass(cls, target)

    @classmethod
    def on_top(cls) -> bool:
        """Nothing is built on a top layer, so nothing outside it may import it."""
        return not any(
            member is not cls and member.may_import(cls) for member in Layer.members_with(Layer)
        )


class Wire(Layer):
    prefixes = ("bus_", "wire_", "message_", "delivery_", "private_")


class Registry(Wire):
    prefixes = ("registry_", "registration_", "owner_", "thread_", "channel_")


class Coordination(Registry):
    prefixes = ("goal_", "attempt_", "coordination_", "wake_")


class Context(Coordination):
    prefixes = ("working_", "context_segments", "instructions")


class Turns(Context):
    prefixes = ("turn_", "input_", "session_", "selected_")


class Runtime(Turns):
    prefixes = ("native_", "pi_", "acp_", "agent_", "runtime_", "_pi_helpers", "cli_bridges")


class UiModel(Runtime):
    pass


class Cli(Runtime):
    prefixes = ("cli_", "tool_", "tools")


class Commander(Runtime):
    pass


@dataclass(frozen=True)
class Import(ABC):
    """One import statement inside agent_comms naming another agent_comms module. Its
    class says when the statement runs."""

    source: str
    target: str
    line: int

    loads: typing.ClassVar[bool]
    """Whether loading ``source`` runs it, so loading ``source`` loads ``target``."""

    @classmethod
    def in_function(cls) -> type[Import]:
        """The class of an import written one function deeper."""
        return CallTimeImport


class LoadTimeImport(Import):
    """At module level: it runs when its module loads."""

    loads = True


class CallTimeImport(Import):
    """Inside a function body: it runs when the function is called."""

    loads = False


class CheckerImport(Import):
    """Under ``if TYPE_CHECKING:``: only a type checker reads it."""

    loads = False

    @classmethod
    def in_function(cls) -> type[Import]:
        return cls


class ModuleReferences(ast.NodeVisitor, ABC):
    """Reads a literal import_module call as an import of the module it names."""

    @abstractmethod
    def record(self, node: ast.stmt | ast.expr, name: str) -> None: ...

    def visit_Call(self, node: ast.Call) -> None:
        match node:
            case ast.Call(
                func=ast.Attribute(attr="import_module"), args=[ast.Constant(str() as name), *_]
            ):
                self.record(node, name)
        self.generic_visit(node)


class ImportNames(ModuleReferences):
    """Records each agent_comms module one source file names in an import statement
    or a literal import_module call."""

    def __init__(self, package: Package, module: str, path: Path, found: list[Import]) -> None:
        self.package, self.module, self.found = package, module, found
        # relative imports start at the file's own directory, a package's __init__ included
        self.parent = ".".join(path.parent.relative_to(package.src).parts)
        self.runs: type[Import] = LoadTimeImport

    def record(self, node: ast.stmt | ast.expr, *names: str) -> None:
        for name in names:
            if (target := self.package.resolve(name)) is not None:
                self.found.append(self.runs(self.module, target, node.lineno))

    def visit_within(self, runs: type[Import], nodes: Sequence[ast.AST]) -> None:
        outer, self.runs = self.runs, runs
        for node in nodes:
            self.visit(node)
        self.runs = outer

    def visit_scope(self, node: ast.AST, called: Sequence[ast.AST]) -> None:
        """Decorators, defaults and annotations run where the definition runs; only the
        body runs when the function is called."""
        self.visit_within(
            self.runs,
            [child for child in ast.iter_child_nodes(node) if all(child is not c for c in called)],
        )
        self.visit_within(self.runs.in_function(), called)

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.visit_scope(node, node.body)

    # NodeVisitor dispatches on the node's class name; a coroutine's scopes are a function's.
    visit_AsyncFunctionDef = visit_FunctionDef  # noqa: N815

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self.visit_scope(node, [node.body])

    def visit_Module(self, node: ast.Module) -> None:
        self.typing = TypingImports.of(node)
        self.generic_visit(node)

    def visit_If(self, node: ast.If) -> None:
        test = TypingReference(self.typing)
        test.dispatch_sync(node.test)
        if test.is_type_checking:
            self.visit(node.test)
            self.visit_within(CheckerImport, node.body)
            self.visit_within(self.runs, node.orelse)
        else:
            self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        self.record(node, *(alias.name for alias in node.names))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        parts = self.parent.split(".")
        base = ".".join(parts[: len(parts) - node.level + 1]) if node.level else ""
        stem = ".".join(part for part in (base, node.module or "") if part)
        self.record(node, *(f"{stem}.{alias.name}" for alias in node.names))


@dataclass(frozen=True)
class ClientImport:
    """One absolute import statement in a stack client."""

    client: str
    path: str
    line: int
    module: str
    names: tuple[str, ...] = ()

    def __str__(self) -> str:
        return f"{self.client}:{self.path}:{self.line} imports {self.module}"


class ClientImportNames(ModuleReferences):
    """Records each absolute module one client file imports; the package keeps its own."""

    def __init__(self, client: str, path: str, found: list[ClientImport]) -> None:
        self.client, self.path, self.found = client, path, found

    def record(self, node: ast.stmt | ast.expr, name: str, names: tuple[str, ...] = ()) -> None:
        self.found.append(ClientImport(self.client, self.path, node.lineno, name, names))

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.record(node, alias.name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        match node:
            case ast.ImportFrom(module=str(module), level=0, names=[ast.alias(name="*")]):
                self.record(node, module)
            case ast.ImportFrom(module=str(module), level=0):
                self.record(node, module, tuple(alias.name for alias in node.names))


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


class ImportGraph:
    """The modules reached from some modules through a set of import edges. Importing a
    module imports every package above it first, so those are reached too."""

    def __init__(self, edges: Iterable[Import]) -> None:
        self.targets: dict[str, set[str]] = {}
        for edge in edges:
            self.add(edge.source, (edge.target,))

    def add(self, source: str, targets: Iterable[str]) -> None:
        self.targets.setdefault(source, set()).update(targets)

    def reached(self, starts: Iterable[str]) -> set[str]:
        seen: set[str] = set()
        todo = list(starts)
        while todo:
            module = todo.pop()
            if module in seen:
                continue
            seen.add(module)
            parts = module.split(".")
            todo.extend(".".join(parts[:end]) for end in range(1, len(parts)))
            todo.extend(self.targets.get(module, ()))
        return seen


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
    def modules(self) -> dict[str, Path]:
        """Every importable module, the package root included."""
        return {PACKAGE: self.root, **self.files}

    def contains(self, module: str) -> bool:
        """Whether an absolute module name lies inside this package, present or not."""
        return self.src.joinpath(*module.split(".")).is_relative_to(self.root.parent)

    @cached_property
    def imports(self) -> tuple[Import, ...]:
        found: list[Import] = []
        for module, path in self.modules.items():
            ImportNames(self, module, path, found).visit(ast.parse(path.read_text(), str(path)))
        return tuple(sorted(set(found), key=lambda edge: (edge.source, edge.line, edge.target)))

    @cached_property
    def layers(self) -> dict[str, type[Layer]]:
        """Each unit's layer: its longest claim, else the highest layer it imports.

        Highest is by declaration: on the chain that is the inheritance order, and between
        top layers, which import nothing of each other, the later declared one."""
        members = Layer.members_with(Layer)
        position = {layer: index for index, layer in enumerate(members)}
        claimed: dict[str, type[Layer]] = {}
        for unit in self.top_level:
            layer = max(members, key=lambda layer: layer.claim(unit))
            if layer.claim(unit):
                claimed[unit] = layer
        layers = {unit: claimed.get(unit, Wire) for unit in self.top_level}
        changed = True
        while changed:
            changed = False
            for edge in self.unit_imports:
                source, target = self.unit(edge.source), self.unit(edge.target)
                higher = position[layers[target]] > position[layers[source]]
                if source not in claimed and higher:
                    layers[source], changed = layers[target], True
        return layers

    @cached_property
    def unit_imports(self) -> tuple[Import, ...]:
        """Imports between units; the package root is read for reachability only."""
        return tuple(edge for edge in self.imports if edge.source in self.files)

    @cached_property
    def violations(self) -> tuple[Import, ...]:
        """Imports into a layer the importing layer is not built on."""
        return tuple(
            edge
            for edge in self.unit_imports
            if not self.layers[self.unit(edge.source)].may_import(
                self.layers[self.unit(edge.target)]
            )
        )

    @cached_property
    def entry_points(self) -> tuple[str, ...]:
        match tomllib.loads(self.pyproject.read_text()):
            case {"project": {"scripts": dict() as scripts}}:
                return tuple(sorted({target.partition(":")[0] for target in scripts.values()}))
        raise ValueError(f"{self.pyproject} declares no [project.scripts] table")

    def provides(self, module: str) -> bool:
        return module in self.modules

    def client_targets(self, entry: ClientImport) -> tuple[str, ...]:
        """The modules one client import loads: a named submodule, else the module itself."""
        if not self.provides(entry.module):
            return ()
        named = tuple(f"{entry.module}.{name}" for name in entry.names)
        return tuple(name for name in named if name in self.files) or (entry.module,)

    @cached_property
    def client_imports(self) -> tuple[ClientImport, ...]:
        return tuple(
            entry
            for client in self.clients
            for entry in client.imports
            if self.contains(entry.module)
        )

    @cached_property
    def missing(self) -> tuple[ClientImport, ...]:
        """Client imports naming an agent_comms module this tree does not have."""
        return tuple(entry for entry in self.client_imports if not self.provides(entry.module))

    def defined_names(self, module: str) -> frozenset[str]:
        """The names a module binds at its top level, re-exports included."""
        path = self.modules[module]
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
            found += [
                (entry, name)
                for name in entry.names
                if name not in defined and f"{entry.module}.{name}" not in self.files
            ]
        return tuple(found)

    @cached_property
    def client_surface(self) -> dict[str, set[str]]:
        """Each unit a client imports directly, with the clients importing it."""
        surface: dict[str, set[str]] = {}
        for entry in self.client_imports:
            for target in self.client_targets(entry):
                if (unit := self.unit(target)) in self.top_level:
                    surface.setdefault(unit, set()).add(entry.client)
        return dict(sorted(surface.items()))

    @cached_property
    def unreached(self) -> tuple[str, ...]:
        """Units no console script or stack client reaches through static imports,
        package __init__s included."""
        clients = (target for entry in self.client_imports for target in self.client_targets(entry))
        seen = ImportGraph(self.imports).reached([PACKAGE, *self.entry_points, *clients])
        return tuple(unit for unit in self.top_level if not any(self.unit(m) == unit for m in seen))

    def load_graph(self) -> ImportGraph:
        """The imports that run while modules load: a cycle through them deadlocks an import."""
        return ImportGraph(edge for edge in self.imports if edge.loads)

    @staticmethod
    def joined(words: Iterable[str]) -> str:
        """Words as an English list: ``a``, ``a and b``, ``a, b and c``."""
        *rest, last = words
        return f"{', '.join(rest)} and {last}" if rest else last

    def markdown(self) -> str:
        lines = [
            "# Module layers",
            "",
            "Generated by `python tools/module_layers.py` for the tree it ran on.",
            "",
            "Layers in order: "
            + ", ".join(f"`{layer.declared_name}`" for layer in Layer.members_with(Layer))
            + ". Each layer imports only the layers before it; "
            + self.joined(
                f"`{layer.declared_name}`" for layer in Layer.members_with(Layer) if layer.on_top()
            )
            + " sit on top, and nothing outside a top layer imports it. The layer classes in "
            "`tools/module_layers.py` declare the claiming prefixes; a module no prefix "
            "claims takes the highest layer it imports.",
            "",
            f"{len(self.top_level)} top-level units ({len(self.subpackages)} of them subpackages); "
            f"{len(self.violations)} violating imports; {len(self.unreached)} deletion candidates.",
            "",
        ]
        for layer in Layer.members_with(Layer):
            units = [unit for unit in self.top_level if self.layers[unit] is layer]
            lines += [f"## {layer.declared_name} ({len(units)})", ""]
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
                f"| `{path}:{edge.line}` | `{edge.target}` "
                f"| {source.declared_name} | {target.declared_name} |"
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
    parser.add_argument(
        "--report", type=Path, help="with --check, also write the layer report to this file"
    )
    arguments = parser.parse_args()
    with tempfile.TemporaryDirectory() as clones:
        package = Package(ROOT, StackClient.fetched(ROOT / "stack", Path(clones)))
        if not arguments.check:
            sys.stdout.write(package.markdown())
            return 0
        if arguments.report:
            arguments.report.write_text(package.markdown())
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
