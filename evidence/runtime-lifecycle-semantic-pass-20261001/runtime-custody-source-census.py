"""Consume NRA's original inspection/inheritance/flow APIs, without detectors.

Analysis recipe only: this does not run application code or behavioral tests.
"""
import hashlib
import json
import subprocess
from pathlib import Path

from nominal_refactor_advisor.ast_tools import parse_python_modules
from nominal_refactor_advisor.json_reports import json_report_object
from nominal_refactor_advisor.product_flow_authority import SourceProductFlowRepository
from nominal_refactor_advisor.semantic_inspection import inspect_modules

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "src" / "agent_comms"
OUTPUT = ROOT / "evidence/runtime-lifecycle-semantic-pass-20261001/nra-custody-caller-census.json"
OWNERS = frozenset({
    "NativeSessionIdentity", "NativeCustody", "PiSessionChild", "PersistentPiSession",
    "TurnSession", "TrackedTurnSession", "SelectedSession", "RegistryOwner",
    "ParticipantOwner", "NativeInputRecord", "NativeInputContext", "NativeRuntimeInput",
    "NativeContextProof", "NativePiRpcLaunch", "PromptBinding", "NativeSendStage",
    "SourceCoverage", "NativeSourceCursor", "FreshPrivateSession",
    "RecoveryMonitorCapability", "SelectedSummarySource", "SelectedSummarySlot",
    "SessionRevision", "NativeEvidenceRead", "NativeEvidenceScope", "ContextBudget",
    "CompactionPolicy", "ReplayAssessments", "TurnPhase", "CoordinationSegment",
    "NativeContextReference", "NativeAdmissionEpoch", "Registration", "ThreadManagement",
    "PrivateSendAdmission", "SelectedParticipant", "SelectedRequest",
})

modules = tuple(parse_python_modules(SOURCE, parse_workers=1, use_parse_cache=False))
print(f"Parsed {len(modules)} production modules", flush=True)
report = inspect_modules(modules, findings=())
repo = SourceProductFlowRepository.from_modules(modules)
index = repo.class_index
roots = tuple(c for c in report.classes if c.name in OWNERS)
symbols = set()
for declaration in roots:
    symbol = index.symbol_for(file_path=declaration.file_path, qualname=declaration.qualname)
    if symbol is not None:
        symbols.add(symbol)
        symbols.update(index.descendant_symbols(symbol))
classes = tuple(c for c in report.classes if index.symbol_for(
    file_path=c.file_path, qualname=c.qualname) in symbols)
method_ids = {method for c in classes for method in c.method_ids}
methods = tuple(f for f in report.functions if f.target_id in method_ids)
names = {c.name for c in classes} | {f.name for f in methods}
imports = tuple(i for i in report.imports if names.intersection(i.imported_names))
candidates = tuple(c for c in report.calls if c.callee.rsplit(".", 1)[-1] in names)
print(f"Original declarations {len(classes)}; method candidates {len(candidates)}", flush=True)

edges = []
for context in repo.iter_flow_contexts():
    for call in context.flow.calls:
        if call.target.terminal_name not in names:
            continue
        resolution = repo.resolve_function_call(context, call)
        edges.append({
            "caller": context.owner_symbol,
            "module": context.module_name,
            "line": call.line,
            "terminal": call.target.terminal_name,
            "possible_symbols": list(resolution.target_resolution.possible_symbols),
            "resolved_function": (resolution.resolved_call.callee.identity.symbol
                if resolution.resolved_call is not None else None),
            "resolution_kind": type(resolution).__name__,
        })

def local(value):
    return str(value).replace(str(ROOT) + "/", "")

def record(value):
    result = json_report_object(value)
    if "file_path" in result:
        result["file_path"] = local(result["file_path"])
    return result

files = sorted(SOURCE.rglob("*.py"))
data = {
    "head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
    "source_is_working_tree": True,
    "method": "NRA parse_python_modules + inspect_modules(findings=()) + original class_index + resolve_function_call",
    "scope": "all production src/agent_comms Python modules; native JavaScript and Toad require separate boundary closure",
    "parse_modules": len(modules),
    "authored_files": len(files),
    "file_sha256": {local(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
    "requested_existing_owner_names": sorted(OWNERS),
    "root_declarations": [record(c) for c in roots],
    "inherited_declarations": [record(c) for c in classes],
    "inheritance": {s: list(index.ancestor_symbols(s)) for s in sorted(symbols)},
    "declared_methods": [record(f) for f in methods],
    "imports": [record(i) for i in imports],
    "same_named_call_candidates": [record(c) for c in candidates],
    "flow_call_resolution": edges,
    "limits": [
        "Same-named method candidates are not semantic ownership claims.",
        "Open/dynamic callees remain explicit; absence of an exact edge does not imply no consumer.",
        "Filesystem location/resource, stored original proof, and live selection require source reasoning.",
        "No detector scan, product execution, test or provider call occurred.",
    ],
}
OUTPUT.parent.mkdir(parents=True, exist_ok=True)
OUTPUT.write_text(json.dumps(data, separators=(",", ":")) + "\n")
print(json.dumps({"output": local(OUTPUT), "roots": len(roots), "classes": len(classes),
    "methods": len(methods), "call_candidates": len(candidates), "flow_edges": len(edges),
    "resolved_edges": sum(e["resolved_function"] is not None for e in edges)}), flush=True)
