"""One bounded source-evidence query; uses refactor-audit's existing Package parser.

This is not a detector, migration engine, type resolver, or acceptance test.
Names/attributes are lexical leads. The accompanying receipt adjudicates them.
Invoke with the project's Python and the refactor-audit scripts directory.
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import sys


# These are query selectors, not an application registry or message taxonomy.
NOMINAL = frozenset("""
SelectedSummaryAttempt SelectedSummarySource SelectedSource SelectedAdmissionSource
CompactionSource InputProvenance StoredInput InputTaskFact ExactTaskFact
RetainedTaskFacts NativeInputConstraintPin SelectedCommitReference
SummaryOperationIdentity CompactionOperation CompactionPublication PrivateRawInput
EnrolledPrivateSession JournalTable SessionJournalHistory UnresolvedJournalHistory
CompactionJournal FreshPrivateSession FreshCoverageIdentity NativeWitness
Provenance TextDigest InputDocument SelectedCompactionOutcome CompactionOutcomeSnapshot
NativeRuntimeInput CurrentNativeCursor JournalProvenance Message
CarriedNativeStore NativeSchemaDeclaration NativeSchemaCarryPlan RuntimeNativeFiles
AcquiredRuntimeFiles RuntimeCompactionFiles RuntimeInstallation
FieldCodec FieldRepresentation TypedTable TypedRow DeclaredFamily AutoRegisterMeta RegistryConfig
""".split())
MEMBERS = frozenset("""
source_json selectedSummarySourceDigest original_digest ingress_key originals
pending_input_key pending_inputs original_sources context_provenance require_source
journal_json envelope request selected_attempt require_summary_link
admit_original original_has_started compaction_files compaction_columns
compaction_tables members_with unchanged_by require_original
digest origin subject context_sources original_wording_provenance
original_input_sources capture_requests original_compaction_requests
carry_compaction carry_coordination carry_binding prepare install carry publish acquire
""".split())
LITERALS = NOMINAL | MEMBERS | {"selected_summary_attempts", "compaction-commits.sqlite3"}


def project(package):
    modules = package.modules
    definitions = defaultdict(list)
    classes = [(module, node) for module in modules for node in ast.walk(module.tree)
               if isinstance(node, ast.ClassDef)]
    selected = set(NOMINAL)
    # Conservative syntactic inheritance closure, not a resolved runtime MRO.
    while True:
        discovered = {node.name for _, node in classes
                      if any(isinstance(part, ast.Name) and part.id in selected
                             or isinstance(part, ast.Attribute) and part.attr in selected
                             for base in node.bases for part in ast.walk(base))}
        if discovered <= selected:
            break
        selected.update(discovered)
    events, imports, declarations = [], [], []
    for module in modules:
        parents = {child: parent for parent in ast.walk(module.tree)
                   for child in ast.iter_child_nodes(parent)}
        aliases = set(selected)
        for node in ast.walk(module.tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                imported = [{"name": item.name, "as": item.asname} for item in node.names]
                imports.append({"path": module.path, "line": node.lineno,
                                "syntax": ast.unparse(node), "bindings": imported})
                aliases.update(item.asname or item.name for item in node.names
                               if item.name in selected)
        for node in ast.walk(module.tree):
            if isinstance(node, ast.ClassDef) and node.name in selected:
                record = {"path": module.path, "line": node.lineno, "name": node.name,
                          "bases": [ast.unparse(base) for base in node.bases],
                          "annotations": [ast.unparse(item) for item in node.body
                                          if isinstance(item, ast.AnnAssign)],
                          "members": [{"name": item.name, "line": item.lineno}
                                      for item in node.body if isinstance(item, (ast.FunctionDef,
                                      ast.AsyncFunctionDef, ast.ClassDef))]}
                declarations.append(record)
                definitions[node.name].append({"path": module.path, "line": node.lineno})
            kind = ""
            tokens = []
            if isinstance(node, ast.Name) and node.id in aliases | MEMBERS:
                kind, tokens = type(node.ctx).__name__, [node.id]
            elif isinstance(node, ast.Attribute) and node.attr in selected | MEMBERS:
                kind, tokens = type(node.ctx).__name__, [node.attr]
            elif isinstance(node, ast.keyword) and node.arg in MEMBERS:
                kind, tokens = "keyword-write-or-forward", [node.arg]
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                tokens = sorted(token for token in LITERALS if token in node.value)
                if tokens:
                    kind = "literal-boundary-or-documentation"
            elif isinstance(node, ast.Call):
                tokens = sorted({part.id for part in ast.walk(node.func)
                                 if isinstance(part, ast.Name) and part.id in aliases}
                                | {part.attr for part in ast.walk(node.func)
                                   if isinstance(part, ast.Attribute) and part.attr in selected | MEMBERS})
                if tokens:
                    kind = "call"
            elif isinstance(node, (ast.Compare, ast.If, ast.IfExp, ast.While, ast.Match)):
                condition = node.subject if isinstance(node, ast.Match) else (
                    node.test if isinstance(node, (ast.If, ast.IfExp, ast.While)) else node)
                tokens = sorted({part.id for part in ast.walk(condition)
                                 if isinstance(part, ast.Name) and part.id in aliases | MEMBERS}
                                | {part.attr for part in ast.walk(condition)
                                   if isinstance(part, ast.Attribute) and part.attr in selected | MEMBERS})
                if tokens:
                    kind = "decision-or-check"
            if not kind:
                continue
            owner = []
            parent = node
            while parent in parents:
                parent = parents[parent]
                if isinstance(parent, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    owner.append(parent.name)
            syntax = ast.unparse(node)
            events.append({"path": module.path, "line": node.lineno,
                           "owner": ".".join(reversed(owner)), "kind": kind,
                           "tokens": tokens, "syntax": syntax[:600],
                           "syntax_truncated": len(syntax) > 600})
    return {"revision": package.rev, "root": package.root,
            "parsed_modules": len(modules), "unparsed": list(package.unparsed),
            "module_inventory": [{"path": module.path,
                                  "sha256": hashlib.sha256(module.text.encode()).hexdigest()}
                                 for module in modules],
            "definitions": dict(sorted(definitions.items())),
            "nonunique_definitions": {name: sites for name, sites in definitions.items() if len(sites) > 1},
            "syntactic_family_names": sorted(selected), "declarations": declarations,
            "imports": imports, "package_import_targets": sorted(package.imported),
            "events": events, "event_counts": dict(Counter(event["kind"] for event in events))}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--audit-scripts", required=True, type=Path)
    parser.add_argument("--pins", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    sys.path.insert(0, str(args.audit_scripts))
    from audit.findings import Package, ParsedModule
    from audit.repository import Repository

    pins = json.loads(args.pins.read_text())
    result = {"classification": "lexical-AST-source-evidence-not-dynamic-proof",
              "selectors": {"nominal": sorted(NOMINAL), "members": sorted(MEMBERS)},
              "parser": str(args.audit_scripts / "audit/findings.py"),
              "snapshots": []}
    for pin in pins:
        if "installed_directory" in pin:
            directory = Path(pin["installed_directory"])
            modules, unparsed = [], []
            # Package.load is a git API. A wheel dependency has no git source:
            # retain the same ParsedModule/Package evidence, recording failures.
            for path in sorted(directory.rglob("*.py")):
                text = path.read_text()
                relative = path.relative_to(directory.parent)
                try:
                    tree = ast.parse(text)
                except SyntaxError:
                    unparsed.append(str(relative))
                    continue
                modules.append(ParsedModule(str(relative), text, tree,
                                             ".".join(relative.with_suffix("").parts)))
            package = Package(Repository(directory), pin["revision"], directory.name,
                              tuple(modules), tuple(unparsed))
            snapshot = {**pin, "roots": [project(package)]}
            result["snapshots"].append(snapshot)
            print(pin["label"], len(modules), "parsed;", len(unparsed), "omitted", flush=True)
            continue
        repo = Repository(Path(pin["repo"]))
        snapshot = {**pin, "roots": [project(Package.load(repo, pin["revision"], root))
                                    for root in pin["roots"]]}
        result["snapshots"].append(snapshot)
        print(pin["label"], sum(root["parsed_modules"] for root in snapshot["roots"]),
              "parsed;", sum(len(root["unparsed"]) for root in snapshot["roots"]), "omitted", flush=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    if any(root["unparsed"] for snapshot in result["snapshots"] for root in snapshot["roots"]):
        raise SystemExit("Parse omissions require disposition; no zero-by-omission")


if __name__ == "__main__":
    main()
