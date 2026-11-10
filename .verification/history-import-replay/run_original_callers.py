"""Apply native import replay to an exact historical Toad source copy."""
from __future__ import annotations

import argparse
import difflib
import faulthandler
import time
import json
import sys
from pathlib import Path

from nominal_refactor_advisor.ast_tools import parse_python_modules
from nominal_refactor_advisor.audit.repository import Repository
from nominal_refactor_advisor.codemod import (
    CodemodSourceSnapshot,
    RefactorRecipe,
    ReplayGitModuleImportsOperation,
)

parser = argparse.ArgumentParser()
parser.add_argument("provider_repository", type=Path)
parser.add_argument("caller_repository", type=Path)
parser.add_argument("source_revision")
parser.add_argument("target_revision")
parser.add_argument("caller_revision")
parser.add_argument("private_root", type=Path)
args = parser.parse_args()
provider = Repository(args.provider_repository)
caller = Repository(args.caller_repository)
if args.private_root.exists():
    for relative, source in caller.sources(
        args.caller_revision, set(caller.python_paths(args.caller_revision, "src"))
    ):
        if (args.private_root / relative).read_bytes() != source:
            raise RuntimeError(f"Existing private source differs: {relative}")
else:
    args.private_root.mkdir(parents=True)
    caller.extract(args.caller_revision, args.private_root, "src")
faulthandler.dump_traceback_later(20, repeat=True)
started = time.monotonic()
operation = ReplayGitModuleImportsOperation(
    provider_repository=str(args.provider_repository), source_revision=args.source_revision,
    target_revision=args.target_revision, caller_root=str(args.private_root),
)
modules = parse_python_modules(args.private_root / "src")
print(f"parsed {len(modules)} modules: {time.monotonic()-started:.3f}s", file=sys.stderr, flush=True)
snapshot = CodemodSourceSnapshot.from_modules(modules)
print(f"snapshot: {time.monotonic()-started:.3f}s", file=sys.stderr, flush=True)
result = RefactorRecipe("repair-original-installed-toad-imports").with_operation(operation).simulate(snapshot)
print(f"simulated: {time.monotonic()-started:.3f}s", file=sys.stderr, flush=True)
if not result.is_clean:
    raise RuntimeError(str(result))
changed = result.simulation.rewritten_sources
report = {
    "provider_source": operation.source_revision,
    "provider_target": operation.target_revision,
    "caller_revision": caller.revision(args.caller_revision),
    "changed_files": sorted(changed),
    "diffs": {path: "".join(difflib.unified_diff(
        snapshot.sources_by_file_path[path].splitlines(keepends=True),
        source.splitlines(keepends=True), fromfile=path, tofile=path,
    )) for path, source in changed.items()},
    "app_imports_before_apply": sorted(name for name in sys.modules
        if name == "agent_comms" or name.startswith("agent_comms.")
        or name == "toad" or name.startswith("toad.")),
}
assert not report["app_imports_before_apply"], report
result.apply()
faulthandler.cancel_dump_traceback_later()
report["applied"] = True
print(json.dumps(report, indent=2))
