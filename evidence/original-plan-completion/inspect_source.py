"""Read a committed candidate; report original-plan witnesses without importing it.

This is source inspection, not an architectural or behavior test. No runtime,
live document, provider, registry or worktree file is written by this script.
"""

import ast
import io
import json
import subprocess
import sys
import tokenize


root, revision = sys.argv[1:]


def git(*args):
    return subprocess.check_output(["git", "-C", root, *args], text=True)


revision = git("rev-parse", revision).strip()
paths = git("ls-tree", "-r", "--name-only", revision, "src/agent_comms").splitlines()
sources = {
    path: git("show", f"{revision}:{path}") for path in paths if path.endswith(".py")
}
obsolete = {
    "run_one_sealed_claim", "WakeClaim", "ClaimState", "ProjectionRecord",
    "TurnClaimFence", "ThreadRegistry", "_JsonLineReader", "_ACTIVE_STEERING_TASKS",
    "claim_local_turn", "claim_live_turn_with_epoch", "claim_live_turn_with_generation",
    "claim_live_turn_with_admission", "to_display_wire", "from_legacy",
    "WireExportScopeKind", "WireExportLimitKind", "accepts_declared_fields",
    "invalid_payload_message",
}
result = {
    "candidate": revision,
    "method": "AST/token inspection of committed source, no tests or imports",
    "obsolete_identifier_hits": [],
    "epoch_identifier_hits": [],
    "retired_modules_present": [
        name for name in ("operations.py", "declarations.py", "resource_claims.py",
                          "state_tags.py", "claim_states.py", "bus_durability.py")
        if f"src/agent_comms/{name}" in sources
    ],
    "retired_aggregate_imports": [],
    "modules_over_1000_lines": [],
    "functions_over_100_lines": [],
    "remaining_lease_assignment_names": [],
}
lease_paths = {
    "turn_runner.py", "owned_turn.py", "turn_progress.py", "manual_compaction_bridge.py",
    "goal_failure_observation.py", "acp.py",
}
for path, source in sources.items():
    tree = ast.parse(source)
    lines = len(source.splitlines())
    if lines > 1000:
        result["modules_over_1000_lines"].append([path, lines])
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type != tokenize.NAME:
            continue
        witness = [path, token.start[0], token.string]
        if token.string in obsolete:
            result["obsolete_identifier_hits"].append(witness)
        if "epoch" in token.string:
            result["epoch_identifier_hits"].append(witness)
        if path.split("/")[-1] in lease_paths and token.string in {"claim", "turn_claim"}:
            result["remaining_lease_assignment_names"].append(witness)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[-1] in {
            "operations", "declarations"
        }:
            result["retired_aggregate_imports"].append([path, node.lineno, node.module])
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            span = node.end_lineno - node.lineno + 1
            if span > 100:
                result["functions_over_100_lines"].append([path, node.lineno, node.name, span])
json.dump(result, sys.stdout, indent=2)
print()
