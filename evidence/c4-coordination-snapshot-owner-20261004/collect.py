"""Project the original audit Package facts; no replacement AST collector."""
import argparse, json, sys
from pathlib import Path
parser = argparse.ArgumentParser()
parser.add_argument("revision")
parser.add_argument("output", type=Path)
args = parser.parse_args()
sys.path.insert(0, "/home/ts/wt/nra-skill-publish-20261002/skills/refactor-audit/scripts")
from audit.findings import Package, GodClass
from audit.repository import Repository
repo = Repository(Path.cwd())
related = {"coordination_snapshot", "viewer_snapshot", "CoordinationSnapshot", "message_notifications_for_references", "MessageNotification"}
roots = {}
for root in ("src/agent_comms", "tests", "tools"):
    package = Package.load(repo, args.revision, root)
    roots[root] = {
        "parsed": len(package.modules), "unparsed": package.unparsed,
        "consumers": [{"function": fact.qualname, "calls": sorted(fact.constructed & related)}
                      for facts in package.functions.values() for fact in facts
                      if fact.constructed & related],
    }
    if root == "src/agent_comms":
        roots[root]["god_classes"] = [finding.line() for finding in GodClass.detect(package)]
        roots[root]["owner_sizes"] = {name: size for name, size in package.sizes.items()
            if name in {"src/agent_comms/history_views.py::HistoryViews", "src/agent_comms/presentation.py::CoordinationSnapshot", "src/agent_comms/presentation.py::MessageNotification"}}
        roots[root]["owner_functions"] = [
            {"function": fact.qualname, "size": fact.size, "calls": sorted(fact.constructed),
             "dispatch": {name: sorted(values) for name, values in fact.dispatch.items()},
             "keys": {name: sorted(values) for name, values in fact.keys.items()}}
            for path, facts in package.functions.items()
            if path in {"src/agent_comms/history_views.py", "src/agent_comms/presentation.py"}
            for fact in facts]
        roots[root]["fields"] = {name: sorted(fields) for name, fields in package.class_fields.items()
            if name.startswith("src/agent_comms/presentation.py::")}
args.output.write_text(json.dumps({"revision": repo.git("rev-parse", args.revision).strip(),
    "collector": "original audit.findings.Package, FunctionFacts, GodClass", "roots": roots,
    "limits": "Calls are lexical callee names, not receiver/runtime resolution. Related declarations, reads, writes and receiver identities were source-read. No dynamic behavior claim."}, indent=2) + "\n")
print(json.dumps({root: {"parsed": value["parsed"], "unparsed": value["unparsed"]} for root, value in roots.items()}))
