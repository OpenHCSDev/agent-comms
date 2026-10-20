"""One-shot source migration from dataclass metadata to typed Annotated directives.

Run from a checkout: python tools/migrate_field_directives.py --write [src tools tests].
The old strings belong only to this source-format boundary, never to a runtime reader.
"""
from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
from pathlib import Path


SPECIAL_NAMES = {
    "sql": None,
    "wire_nonnull": "WireNonNull",
    "registration_inheritance": "RegistrationPolicy",
    "thread_slice": "SlicePolicy",
    "native": "NativeScope",
    "runtime": "RuntimeScope",
    "paths": "WatchPaths",
    "views": "WatchViews",
    "context_binding": "ContextSource",
}


def directive_name(key: str) -> str:
    return SPECIAL_NAMES.get(key) or "".join(part.capitalize() for part in key.split("_"))


@dataclass
class Source:
    path: Path
    text: str

    def __post_init__(self) -> None:
        self.tree = ast.parse(self.text, filename=str(self.path))
        self.lines = self.text.splitlines(keepends=True)
        self.offsets = [0]
        for line in self.lines:
            self.offsets.append(self.offsets[-1] + len(line))
        self.edits: list[tuple[int, int, str]] = []
        self.imports: set[str] = set()
        self.changed = 0
        owner = Path(__file__).resolve().parents[1] / "src/agent_comms/field_codec.py"
        self.directive_types = {node.name for node in ast.parse(owner.read_text()).body
                                if isinstance(node, ast.ClassDef)}

    def start(self, node: ast.AST) -> int:
        # AST columns are UTF-8 bytes, while source edits use Python string offsets.
        return self.offsets[node.lineno - 1] + len(
            self.lines[node.lineno - 1].encode()[:node.col_offset].decode())

    def end(self, node: ast.AST) -> int:
        return self.offsets[node.end_lineno - 1] + len(
            self.lines[node.end_lineno - 1].encode()[:node.end_col_offset].decode())

    def code(self, node: ast.AST) -> str:
        return self.text[self.start(node):self.end(node)]

    def directive(self, key: str, value: ast.AST) -> str:
        if key == "sql":
            return self.code(value)
        if key == "wire_registry_probe":
            # The codec test's deliberately unknown instruction remains unknown.
            return "object()"
        self.imports.add("from agent_comms import field_codec")
        return f"field_codec.{directive_name(key)}({self.code(value)})"

    def field_call(self, function: str, keywords: list[tuple[str, ast.AST | str]]) -> str:
        if not keywords:
            return ""
        return f" = {function}(" + ", ".join(
            f"{key}={value if isinstance(value, str) else self.code(value)}"
            for key, value in keywords) + ")"

    def migrate(self) -> str:
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.AnnAssign) or not isinstance(node.value, ast.Call):
                continue
            call = node.value
            name = call.func.id if isinstance(call.func, ast.Name) else ""
            arguments = {key.arg: key.value for key in call.keywords}
            metadata = arguments.get("metadata")
            extras: list[str] = []
            default = ""
            if isinstance(metadata, ast.Dict):
                if any(not isinstance(key, ast.Constant) or not isinstance(key.value, str)
                       for key in metadata.keys):
                    raise ValueError(f"{self.path}:{node.lineno}: dynamic field metadata")
                known, foreign = [], []
                for key, value in zip(metadata.keys, metadata.values):
                    owned = (key.value in ("sql", "wire_registry_probe")
                             or directive_name(key.value) in self.directive_types)
                    (known if owned else foreign).append((key, value))
                extras = [self.directive(key.value, value) for key, value in known]
                retained = [(key.arg, key.value) for key in call.keywords if key.arg != "metadata"]
                if foreign:
                    # Other libraries' metadata is their external contract, not ours.
                    retained.append(("metadata", "{" + ", ".join(
                        f"{key.value!r}: {self.code(value)}" for key, value in foreign) + "}"))
                default = self.field_call(self.code(call.func), retained)
            elif (isinstance(metadata, ast.Call) and isinstance(metadata.func, ast.Name)
                  and metadata.func.id == "saved_as"):
                self.imports.add("from agent_comms import field_codec")
                extras = [f"field_codec.WireName({self.code(metadata.args[0])})"]
                default = self.field_call(self.code(call.func), [
                    (key.arg, key.value) for key in call.keywords if key.arg != "metadata"
                ])
            elif name == "option":
                self.imports.update(("from agent_comms import field_codec", "import dataclasses"))
                parser = []
                if call.args:
                    flags = ", ".join(self.code(arg) for arg in call.args)
                    parser.append(f"flags=({flags},)")
                if "group" in arguments:
                    parser.append(f"group={self.code(arguments['group'])}")
                if "parser_default" in arguments:
                    parser.append(f"default=field_codec.GivenArgumentDefault({self.code(arguments['parser_default'])})")
                elif "parser_default_factory" in arguments:
                    parser.append(f"default=field_codec.FactoryArgumentDefault({self.code(arguments['parser_default_factory'])})")
                constructor = []
                options = []
                hints = {"normalize", "wire_name", "multiline", "target_bound", "editor_format", "editor", "catalog"}
                for key, value in arguments.items():
                    if key in ("default", "default_factory"):
                        constructor.append((key, value))
                    elif key in hints:
                        extras.append(self.directive(key, value))
                    elif key not in ("group", "parser_default", "parser_default_factory"):
                        if key is None:
                            options.append("**" + self.code(value))
                        else:
                            options.append(f"{key!r}: {self.code(value)}")
                if options:
                    parser.append("options={" + ", ".join(options) + "}")
                extras.append("field_codec.Argument(" + ", ".join(parser) + ")")
                default = self.field_call("dataclasses.field", constructor)
            elif name == "_omitted_when_absent":
                self.imports.update(("from agent_comms import field_codec", "import dataclasses"))
                extras = ["field_codec.WireOmitDefault()"]
                extras.extend(self.directive(key, value) for key, value in arguments.items() if key != "default")
                value = call.args[0] if call.args else arguments.get("default")
                default = (self.field_call("dataclasses.field", [("default", value)])
                           if value is not None else " = dataclasses.field(default=None)")
            elif name == "tool_field":
                self.imports.add("import dataclasses")
                builder = [self.code(arg) for arg in call.args]
                builder.extend(f"{key}={self.code(value)}" for key, value in arguments.items()
                               if key not in ("default", "default_factory"))
                extras = ["*tool_field(" + ", ".join(builder) + ")"]
                default = self.field_call("dataclasses.field", [
                    (key, value) for key, value in arguments.items() if key in ("default", "default_factory")
                ])
            elif name == "revision_source":
                extras = ["*" + self.code(call)]
            else:
                continue
            if not extras:
                continue
            self.imports.add("import typing")
            annotation = self.code(node.annotation)
            if (isinstance(node.annotation, ast.Subscript)
                    and ast.unparse(node.annotation.value) in ("Annotated", "typing.Annotated")):
                last = node.annotation.slice.elts[-1]
                position = self.end(last) - self.start(node.annotation)
                annotation = annotation[:position] + ", " + ", ".join(extras) + annotation[position:]
            else:
                annotation = "typing.Annotated[" + annotation + ", " + ", ".join(extras) + "]"
            self.edits.append((self.start(node.annotation), self.end(call), annotation + default))
            self.changed += 1
        if not self.edits:
            return self.text
        imports = self.imports - {ast.unparse(node) for node in self.tree.body
                                  if isinstance(node, (ast.Import, ast.ImportFrom))}
        position = 0
        for node in self.tree.body:
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                position = self.offsets[node.end_lineno]
            elif isinstance(node, ast.ImportFrom) and node.module == "__future__":
                position = self.offsets[node.end_lineno]
            else:
                break
        self.edits.append((position, position, "\n" + "\n".join(sorted(imports)) + "\n"))
        result = self.text
        for start, end, replacement in sorted(self.edits, reverse=True):
            result = result[:start] + replacement + result[end:]
        ast.parse(result, filename=str(self.path))
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("roots", nargs="*", default=["src", "tools", "tests"])
    options = parser.parse_args()
    total = 0
    updates = []
    for root in options.roots:
        target = Path(root)
        paths = [target] if target.is_file() else target.rglob("*.py")
        for path in sorted(paths):
            if "skills" in path.parts or path == Path(__file__):
                continue
            source = Source(path, path.read_text())
            result = source.migrate()
            if source.changed:
                print(f"{path}: {source.changed}")
                total += source.changed
                updates.append((path, result))
    if options.write:
        # Validate the whole batch before changing any source file.
        for path, result in updates:
            path.write_text(result)
    print(f"fields converted: {total}")


if __name__ == "__main__":
    main()
