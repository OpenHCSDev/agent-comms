"""Scoped evidence adapter using the authoritative refactor-audit Package AST owner.

No copied PR60 helpers, new parser, product codec or migration machinery.
"""
import ast
import json
import subprocess
import sys
from pathlib import Path

ARCHIVE = "/home/ts/code/projects/nominal-refactor-advisor/skills/refactor-audit.skill"
sys.path.insert(0, ARCHIVE + "/refactor-audit/scripts")
from audit.findings import Package, _absolute
from audit.repository import Repository

OUTPUT = Path(__file__).resolve().parent
CORE = Repository(OUTPUT.parents[2])
TOAD = Repository(Path("/home/ts/wt/toad-receiving-native5-batch490-20261001"))
SEEDS = {
    "Subtask", "SubtaskTaskFact", "CommsSubtaskTool", "TaskBoundaryCompactionReason", "TaskAttachment", "ScopedTaskDeclaration", "ModelTaskDeclaration", "TaskScope", "CommsAuthoredTaskTool", "PiCompactionDecision", "AgentCommsCompactionSettings", "CompactionReason", "InputProvenance", "StoredInput", "InputDocument", "InputBatch", "ChannelInputBatch",
    "OriginalTurnInput", "SelectedSource", "SelectedSourceBatch", "CompactionSource", "HeldCompaction",
    "SelectedSummarySource", "SelectedSummaryAttempt", "SelectedSummaries",
    "SelectedCommitReference", "SelectedSummaryAdmission", "SelectedAdmissionIdentity",
    "SummaryState", "PrivateInputs", "OwnerCompactionCommit", "SelectedSession",
    "SelectedRequest", "PrivateSendAdmission", "NativePreparationResult", "CompactionResult",
    "TurnSession", "TrackedTurnSession", "PersistentPiSession", "NativeContextProof",
    "TurnContext", "ContextManifest", "InputTaskFact", "NativeInputConstraintPin",
    "RetainedTaskFacts", "ExactTaskFact", "InputOrigin", "Provenance", "ContextSegment",
    "RenderedInput", "InputContributionCoordinates", "NativeContextManifestData",
    "NativeIntent", "InputAttempt", "ReservationCheck",
}
ATTRS = {"source_json", "originals", "pending_inputs", "ingress_key", "pending_input_key",
         "original_digest", "require_source", "prepare_context", "resume_prepared",
         "compact_selected", "reservation_check", "interrupted_check", "context_provenance",
         "require_source_coverage", "original_has_started", "consume_bound_original",
         "bind_originals", "journal_json", "adaptive_compaction_enabled",
         "input_sources", "original_inputs", "matches_original_source", "require_retained"}
RETIRED = {"read_compaction_decision", "read_compaction_settings", "adaptive_compaction_enabled"}

if len(sys.argv) > 3:
    OUTPUT = Path(sys.argv[3]).resolve()
    OUTPUT.mkdir(parents=True, exist_ok=True)

for label, revision in [("before", sys.argv[2] if len(sys.argv) > 2 else "a87a7065"), ("after", sys.argv[1] if len(sys.argv) > 1 else "62719ea96dc646d6dff0d717ae747d9bc6ceacb3")]:
    entries = [(CORE, revision, root) for root in ("src", "tests", "tools")]
    toad_revision = TOAD.git("rev-parse", sys.argv[4] if len(sys.argv) > 4 else "HEAD").strip()
    entries.append((TOAD, toad_revision, "src"))
    coverage, modules, omissions = [], [], []
    for repository, rev, root in entries:
        package = Package.load(repository, rev, root)
        paths = repository.git("ls-tree", "-r", "--name-only", rev, "--", root)
        coverage.append({"repository": str(repository.path), "revision": rev, "root": root,
                         "parsed_python": len(package.modules), "unparsed_python": list(package.unparsed),
                         "empty_root": not bool(package.modules)})
        modules.extend((str(repository.path), rev, module) for module in package.modules)
        omissions.extend({"repository": str(repository.path), "revision": rev, "path": path,
                          "reason": "non-Python source; authoritative Package parser does not parse native JavaScript/TypeScript"}
                         for path in paths.splitlines() if Path(path).suffix in {".mjs", ".js", ".ts", ".tsx"})
    # stack is a real dependency root, not silently excluded because it is native code.
    for path in CORE.git("ls-tree", "-r", "--name-only", revision, "--", "stack").splitlines():
        if Path(path).suffix in {".mjs", ".js", ".ts", ".tsx"}:
            omissions.append({"repository": str(CORE.path), "revision": revision, "path": path,
                              "reason": "native AST parser unavailable here; Arendt owns native producer mapping"})
    classes = []
    for repository, rev, module in modules:
        for node in ast.walk(module.tree):
            if isinstance(node, ast.ClassDef):
                classes.append((repository, rev, module, node))
    family = set(SEEDS)
    while True:
        expanded = family | {node.name for _, _, _, node in classes
                             if any(ast.unparse(base).rsplit(".", 1)[-1] in family for base in node.bases)}
        if expanded == family:
            break
        family = expanded
    declarations, consumers, decisions, embedded, imports, constructors = [], [], [], [], [], []
    for repository, rev, module in modules:
        parents = {child: node for node in ast.walk(module.tree) for child in ast.iter_child_nodes(node)}
        aliases = {}
        for node in ast.walk(module.tree):
            if isinstance(node, ast.ImportFrom):
                absolute = _absolute(module.package, node.module, node.level)
                for alias in node.names:
                    aliases[alias.asname or alias.name] = absolute + "." + alias.name
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    aliases[alias.asname or alias.name.split(".")[0]] = alias.name
        for node in ast.walk(module.tree):
            chain, parent = [], parents.get(node)
            while parent is not None:
                if isinstance(parent, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    chain.append(parent.name)
                parent = parents.get(parent)
            context = ".".join(reversed(chain))
            loc = {"repository": repository, "revision": rev, "path": module.path,
                   "line": getattr(node, "lineno", 0), "context": context}
            if isinstance(node, ast.ClassDef) and node.name in family:
                members = [{"name": member.name, "line": member.lineno}
                           for member in node.body if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))]
                fields = [{"name": ast.unparse(member.target), "annotation": ast.unparse(member.annotation),
                           "line": member.lineno} for member in node.body if isinstance(member, ast.AnnAssign)]
                declarations.append(dict(loc, name=node.name, bases=[ast.unparse(base) for base in node.bases],
                                         methods=members, fields=fields))
            if isinstance(node, ast.ImportFrom):
                affected = [alias.name for alias in node.names if alias.name in family or alias.name in RETIRED]
                if affected:
                    imports.append(dict(loc, target=_absolute(module.package, node.module, node.level), names=affected))
            relevant = (isinstance(node, ast.Name) and (node.id in family | RETIRED or aliases.get(node.id, "").rsplit(".", 1)[-1] in family)
                        or isinstance(node, ast.Attribute) and (node.attr in ATTRS
                            or node.attr == "request" and ("attempt" in ast.unparse(node.value) or context.startswith("SelectedSummaryAttempt."))
                            or node.attr in {"envelope", "source"} and ("attempt" in ast.unparse(node.value) or "request" in ast.unparse(node.value))))
            if isinstance(node, ast.Call):
                called = ast.unparse(node.func)
                resolved = aliases.get(called.split(".")[0], called)
                if called.rsplit(".", 1)[-1] in family | RETIRED or resolved.rsplit(".", 1)[-1] in family | RETIRED:
                    constructors.append(dict(loc, callee=called, imported_binding=resolved,
                        positional_arguments=[ast.unparse(argument) for argument in node.args],
                        keyword_arguments=[{"name":keyword.arg, "value":ast.unparse(keyword.value)} for keyword in node.keywords],
                        resolution="syntactic constructor/call; runtime binding not proven"))
            if relevant:
                ancestor, checks = parents.get(node), []
                while ancestor is not None and not isinstance(ancestor, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    if isinstance(ancestor, (ast.If, ast.Compare, ast.BoolOp, ast.Match, ast.Assert)):
                        checks.append({"kind": type(ancestor).__name__, "line": ancestor.lineno,
                                       "expression": ast.unparse(ancestor)[:700]})
                    ancestor = parents.get(ancestor)
                expression = ast.unparse(node)
                consumers.append(dict(loc, expression=expression, syntax=type(node).__name__,
                    operation=type(node.ctx).__name__ if isinstance(node, (ast.Name, ast.Attribute)) else "reference",
                    imported_binding=aliases.get(expression.split(".")[0]),
                    receiver_resolution="lexical import/name only; attribute runtime dispatch is not proven"))
                if checks:
                    decisions.append(dict(loc, reference=expression, enclosing_checks=checks))
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and any(name in node.value for name in SEEDS | RETIRED):
                if "import " in node.value or "from " in node.value:
                    try:
                        embedded_tree = ast.parse(node.value)
                        embedded.append(dict(loc, parse="parsed fixed embedded Python", references=[ast.unparse(n) for n in ast.walk(embedded_tree)
                            if isinstance(n, ast.Name) and n.id in family | RETIRED or isinstance(n, ast.Attribute) and n.attr in ATTRS]))
                    except SyntaxError as error:
                        embedded.append(dict(loc, parse="not parseable as fixed Python", reason=str(error)))
    counts = {}
    for declaration in declarations:
        if declaration["repository"] == str(CORE.path) and declaration["path"].startswith("src/"):
            counts.setdefault(declaration["name"], []).append(declaration["path"] + ":" + str(declaration["line"]))
    numstat = CORE.git("diff", "--numstat", "a87a7065", revision, "--", "src", "stack")
    lines = [line.split("\t") for line in numstat.splitlines()]
    record = {"label": label, "core_revision": CORE.git("rev-parse", revision).strip(),
              "reference_example": "OpenHCS PR60 commit5e8812ee83d0dc8714392445bad3e32fc47a1755 tests/unit/test_cellprofiler_static_deletion_gates.py",
              "tool": ARCHIVE + ":refactor-audit/scripts/audit/findings.py Package.load",
              "coverage": coverage, "non_python_omissions": omissions,
              "limitations": ["AST references enumerate syntax, not dynamic receiver types or runtime MRO resolution",
                              "Inherited families use source base-name closure; imported aliases, metaclasses and conditional definitions remain ambiguous",
                              "Toad committed source is an explicitly identified dependency snapshot, not an installed/runtime acceptance claim",
                              "SDK node_modules and dynamic/generated/f-string code are not parsed by this Python tool; native producer mapping remains with Arendt"],
              "family_declarations": declarations, "production_duplicate_nominal_definitions": {k:v for k,v in counts.items() if len(v)>1},
              "imports": imports, "constructors_and_arguments": constructors, "consumers_and_writes": consumers, "decisions_and_checks": decisions,
              "embedded_python": embedded,
              "production_delta_from_a87": {"added":sum(int(row[0]) for row in lines if row[0].isdigit()),
                                             "deleted":sum(int(row[1]) for row in lines if row[1].isdigit()), "numstat":numstat}}
    OUTPUT.joinpath(label + ".json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps({"label":label,"parsed":sum(c["parsed_python"] for c in coverage),
                      "parse_failures":sum(len(c["unparsed_python"]) for c in coverage),
                      "declarations":len(declarations),"consumer_sites":len(consumers),"checks":len(decisions),
                      "native_omissions":len(omissions),"production_delta":record["production_delta_from_a87"] | {"numstat":"see raw"}}))
