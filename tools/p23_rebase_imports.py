"""Prepare Git three-way inputs using upstream's module moves, not new behavior.

NRA indexes each original valid stage. Only import syntax is normalized before
Git merges the three inputs. Product files and the Git index are not written.
Unresolved body conflicts remain in the retained merged files for source review.
"""

import argparse
import ast
import difflib
import importlib.util
import json
import subprocess
import sys
import tokenize
from dataclasses import asdict, dataclass, field, replace
from io import StringIO
from pathlib import Path, PurePosixPath

from nominal_refactor_advisor.ast_tools import ModuleSyntaxIndex

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / ".verification" / "p23-rebase-inputs" / "current"


def git(*arguments: str, repository: Path | None = None) -> str:
    return subprocess.check_output(
        ("git", *arguments), cwd=repository or ROOT, text=True, timeout=30,
    )


def module_name(filename: str) -> str:
    path = PurePosixPath(filename)
    parts = path.with_suffix("").parts
    if parts[0] == "src":
        parts = parts[1:]
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


@dataclass(frozen=True)
class ModuleMoves:
    paths: dict[str, str]
    modules: dict[str, str]
    target: str
    symbols: dict[tuple[str, str], str] = field(default_factory=dict)

    @classmethod
    def capture(cls) -> "ModuleMoves":
        target_file = Path(git("rev-parse", "--git-path", "rebase-merge/onto").strip())
        target = target_file.read_text().strip()
        return cls.at(target)

    @classmethod
    def at(cls, target: str, repository: Path | None = None) -> "ModuleMoves":
        lines = git(
            "diff", "--name-status", "--find-renames=30%", "--diff-filter=R",
            "7139f1d8b^", target, "--", "src/agent_comms", repository=repository,
        ).splitlines()
        paths = {}
        for line in lines:
            _similarity, old, new = line.split("\t")
            if old.endswith(".py") and new.endswith(".py"):
                paths[old] = new
        return cls(paths, {module_name(old): module_name(new)
                           for old, new in paths.items()}, target)

    def normalize(self, source: str, filename: str, *, changed_only: bool = False) -> str:
        """Keep bodies/comments and local bindings; expose equal import answers."""
        name = module_name(filename)
        package = name if filename.endswith("/__init__.py") else name.rpartition(".")[0]
        lines = source.splitlines(keepends=True)
        syntax = ModuleSyntaxIndex.build(ast.parse(source, filename=filename))
        imports = syntax.instances_of(ast.ImportFrom)
        for node in sorted(imports, key=lambda item: item.lineno, reverse=True):
            if node.module == "__future__":
                continue
            original = node.module or ""
            if node.level:
                original = importlib.util.resolve_name(
                    "." * node.level + original, package,
                )
            destination = self.modules.get(original, original)
            changed = node.level or destination != original or any(
                original + "." + alias.name in self.modules for alias in node.names
            ) or any(self.symbols.get((original, alias.name), original) != original
                     for alias in node.names)
            if changed_only and not changed:
                continue
            indentation = " " * node.col_offset
            rendered = []
            for alias in node.names:
                imported_module = self.modules.get(original + "." + alias.name)
                if imported_module is not None:
                    owner, _separator, name = imported_module.rpartition(".")
                    binding = alias.asname or (alias.name if name != alias.name else None)
                    projected = ast.ImportFrom(
                        module=owner, names=[ast.alias(name=name, asname=binding)], level=0,
                    )
                else:
                    projected = ast.ImportFrom(
                        module=self.symbols.get((original, alias.name), destination),
                        names=[alias], level=0,
                    )
                rendered.append(indentation + ast.unparse(projected) + "\n")
            assert node.end_lineno is not None
            original_span = "".join(lines[node.lineno - 1:node.end_lineno])
            comments = [indentation + token.string + "\n"
                        for token in tokenize.generate_tokens(StringIO(original_span).readline)
                        if token.type == tokenize.COMMENT]
            lines[node.lineno - 1:node.end_lineno] = comments + rendered
        return "".join(lines)

    def import_recipe(self) -> dict:
        """Project only import syntax in tracked source/tests through the native DSL."""
        operations = []
        for filename in git("ls-files", "src", "tests").splitlines():
            if not filename.endswith(".py"):
                continue
            before = (ROOT / filename).read_text()
            after = self.normalize(before, filename, changed_only=True)
            if before == after:
                continue
            if before.count("# noqa") != after.count("# noqa"):
                raise ValueError(f"Import projection changed noqa markers in {filename}")
            ast.parse(after, filename=filename)
            operations.append({
                "operation": "patch_target", "file_path": str(ROOT / filename),
                "replacements": [{"old_source": before, "new_source": after}],
            })
        return {"recipes": [{
            "recipe_id": "p23-project-upstream-import-identities",
            "reason": "Upstream Git rename declarations and NRA-indexed original imports determine the module identity mapping. Normalize only moved-module and relative import syntax, preserving local bindings, comments, noqa count, bodies and wire identities. This source projection is not runtime or behavior proof; cold roots and actual consumers must qualify the integrated tree.",
            "operations": operations, "architecture_guards": [], "authority_claims": [],
        }], "architecture_guards": []}


@dataclass(frozen=True)
class MergeResult:
    path: str
    original_path: str
    output: str
    conflicts_before: int
    conflicts_after: int
    returncode: int


def prepare(path: str, moves: ModuleMoves, output: Path, *, sort_imports: bool = False,
            merge_inputs: bool = False) -> MergeResult:
    fallback = {new: old for old, new in moves.paths.items()}.get(path, path)
    base = path if merge_inputs else (
        path if git("ls-tree", "--name-only", "REBASE_HEAD^", "--", path).strip() == path else fallback)
    old = path if merge_inputs else (
        path if git("ls-tree", "--name-only", "REBASE_HEAD", "--", path).strip() == path else fallback)
    destination = output / path
    destination.mkdir(parents=True, exist_ok=True)
    for stage, label, filename in ((1, "base", base), (2, "ours", path), (3, "theirs", old)):
        available = git("ls-files", "--unmerged", "--", path).splitlines()
        stage_exists = any(line.split()[2] == str(stage) for line in available)
        source = moves.normalize(git("show", f":{stage}:{path}") if stage_exists else "", filename)
        if sort_imports:
            sorted_source = subprocess.run(
                (sys.executable, "-m", "ruff", "check", "--isolated", "--select", "I001", "--fix-only",
                 "--stdin-filename", str(ROOT / path), "-"),
                input=source, text=True, capture_output=True, timeout=30, check=True,
            ).stdout
            if source.count("# noqa") != sorted_source.count("# noqa"):
                raise ValueError(f"Ruff changed noqa markers in {label}:{path}")
            source = sorted_source
        (destination / f"{label}.py").write_text(source)
    merged = subprocess.run(
        ("git", "merge-file", "-p", "-L", "upstream", "-L", "base", "-L", "P23",
         str(destination / "ours.py"), str(destination / "base.py"),
         str(destination / "theirs.py")),
        text=True, capture_output=True, timeout=30,
    )
    if merged.returncode < 0 or merged.returncode > 127:
        raise RuntimeError((path, merged.returncode, merged.stderr))
    result = destination / "merged.py"
    result.write_text(merged.stdout)
    return MergeResult(path, old, str(result),
                       (ROOT / path).read_text().count("<<<<<<<"),
                       merged.stdout.count("<<<<<<<"), merged.returncode)


def main() -> None:
    global ROOT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--retained-patches", action="store_true")
    parser.add_argument("--merge-inputs", action="store_true",
                        help="Normalize regular merge stages, retaining missing-side conflicts separately")
    parser.add_argument("--sort-imports", action="store_true")
    parser.add_argument("--favor-p23", action="store_true",
                        help="Keep selected P23 conflict sides in retained three-way inputs")
    parser.add_argument("--favor-ours", action="store_true",
                        help="Keep current-branch conflict sides in retained merge inputs")
    parser.add_argument("--favor-theirs-path", action="append", default=[],
                        help="Take the incoming original declaration for this merge path")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--imports-plan", type=Path)
    parser.add_argument("--target")
    parser.add_argument("--repository", type=Path, default=ROOT)
    parser.add_argument("--moves-repository", type=Path)
    arguments = parser.parse_args()
    ROOT = arguments.repository.resolve()
    output = arguments.output
    output.mkdir(parents=True, exist_ok=True)
    if arguments.imports_plan is not None:
        moves = (ModuleMoves.at(arguments.target, arguments.moves_repository)
                 if arguments.target else ModuleMoves.capture())
        recipe = moves.import_recipe()
        arguments.imports_plan.write_text(json.dumps(recipe, indent=2) + "\n")
        print(json.dumps({"operations": len(recipe["recipes"][0]["operations"])}))
        return
    if arguments.retained_patches:
        report = json.loads((output / "report.json").read_text())
        results = [MergeResult(**row) for row in report["results"]]
    else:
        moves = (ModuleMoves.at(arguments.target, arguments.moves_repository)
                 if arguments.target else ModuleMoves.capture())
        paths = [path for path in git("diff", "--name-only", "--diff-filter=U").splitlines()
                 if path.endswith(".py")]
        skipped = []
        if arguments.merge_inputs:
            for path in tuple(paths):
                stages = {line.split()[2] for line in git("ls-files", "--unmerged", "--", path).splitlines()}
                if not {"2", "3"}.issubset(stages):
                    paths.remove(path)
                    skipped.append(path)
        results = [prepare(path, moves, output, sort_imports=arguments.sort_imports,
                           merge_inputs=arguments.merge_inputs)
                   for path in paths]
        report = {"target": moves.target, "moves": moves.paths, "skipped": skipped,
                  "results": [asdict(result) for result in results]}
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    if arguments.favor_p23 or arguments.favor_ours:
        favored = []
        for result in results:
            if not result.conflicts_after:
                favored.append(result)
                continue
            directory = Path(result.output).parent
            side = "--ours" if arguments.favor_ours and result.path not in arguments.favor_theirs_path else "--theirs"
            merged = subprocess.run(
                ("git", "merge-file", "-p", side, "-L", "upstream", "-L", "base",
                 "-L", "P23", str(directory / "ours.py"), str(directory / "base.py"),
                 str(directory / "theirs.py")),
                text=True, capture_output=True, timeout=30, check=True,
            )
            ast.parse(merged.stdout, filename=result.path)
            Path(result.output).write_text(merged.stdout)
            favored.append(replace(result, conflicts_after=0, returncode=0))
        results = favored
        (output / "favored-report.json").write_text(json.dumps({
            "results": [asdict(result) for result in results],
            "limit": "Git conflict-side selection only; retained main additions need explicit source review.",
        }, indent=2) + "\n")
    patch = []
    residual = []
    clean = []
    for result in results:
        before = (ROOT / result.path).read_text()
        after = Path(result.output).read_text()
        difference = list(difflib.unified_diff(
            before.splitlines(keepends=True), after.splitlines(keepends=True),
            fromfile="a/" + result.path, tofile="b/" + result.path,
        ))
        if result.conflicts_after:
            residual.extend(difference)
            continue
        ast.parse(after, filename=result.path)
        patch.extend(difference)
        clean.append(result.path)
    (output / "clean.patch").write_text("".join(patch))
    (output / "clean.paths").write_text("\n".join(clean) + "\n")
    (output / "residual.patch").write_text("".join(residual))
    print(json.dumps({"files": len(results),
                      "clean_merges": sum(result.conflicts_after == 0 for result in results),
                      "conflicts_before": sum(result.conflicts_before for result in results),
                      "conflicts_after": sum(result.conflicts_after for result in results),
                      "report": str(output / "report.json")}))


if __name__ == "__main__":
    main()
