"""Read the saved Core graph and ask NRA's existing guarded-import proof; never apply."""
from __future__ import annotations

import io
import json
import subprocess
import tarfile
from pathlib import Path

from nominal_refactor_advisor.codemod import CodemodPlanRoot, CodemodSourceSnapshot
from nominal_refactor_advisor.codemod_import_scopes import ModuleImportScope
from nominal_refactor_advisor.lexical_bindings import ImportFromModuleName
from nominal_refactor_advisor.codemod_selector_models import SourceRewriteTarget
from nominal_refactor_advisor.codemod_semantics import CodemodPreflightStatus
from nominal_refactor_advisor.json_reports import json_report_object
from nominal_refactor_advisor.refactors.module_move import PromoteGuardedImportOperation

ARTIFACTS = Path(__file__).resolve().parent
ROOT = ARTIFACTS.parents[1]
HEAD = "df33d6461272fdf5b88dbc02b80612d0503dc753"
TARGET = ROOT / "src/agent_comms/pi_native_backend.py"


def main() -> None:
    archive = subprocess.check_output(["git", "archive", HEAD, "src"], cwd=ROOT)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tree:
        sources = {str(ROOT / item.name): tree.extractfile(item).read().decode()
                   for item in tree.getmembers() if item.isfile() and item.name.endswith(".py")}
    snapshot = CodemodSourceSnapshot.from_source_mapping(sources, analysis_roots=(ROOT / "src",))
    parsed = snapshot.parsed_module_for_source_path(str(TARGET))
    operations = []
    eligible = []
    reports = []
    print(f"Pinned Core {HEAD}; {len(sources)} original source inputs; pre-vocabulary-cut report only.", flush=True)
    for statement in ModuleImportScope.TYPE_CHECKING.import_statements(parsed.module):
        module_name = ImportFromModuleName.from_node(statement).source
        names = tuple(alias.name for alias in statement.names)
        operation = PromoteGuardedImportOperation(
            target=SourceRewriteTarget(file_path=str(TARGET)), module_name=module_name, import_names=names,
        )
        payload = {"operation": operation.operation_key(), "file_path": str(TARGET),
                   "module_name": module_name, "import_names": list(names)}
        operations.append(payload)
        native_reports = operation.preflight_reports(snapshot)
        if all(report.status is CodemodPreflightStatus.PASSED for report in native_reports):
            eligible.append(payload)
        for report in native_reports:
            reports.append(json_report_object(report))
            print(f"{module_name}: {report.message}", flush=True)
    plan = {"stages": [{"recipes": [{"recipe_id": "p23-pi-backend-native-guarded-import-proof",
             "reason": "Existing native cycle-checking promotions on saved df33d646, before the independently owned vocabulary cut. Refusals are declaration-cut obligations, not permission to replace this operation with an unguard patch. No local-import proof or product qualification is claimed.",
             "operations": operations, "architecture_guards": [], "authority_claims": []}], "architecture_guards": []}]}
    # The native payload decoder also checks the authored operation schema.
    CodemodPlanRoot.from_json_value(plan)
    (ARTIFACTS / "pi-backend-precut.promotions.plan.json").write_text(json.dumps(plan, indent=2) + "\n")
    (ARTIFACTS / "pi-backend-precut.native-preflights.json").write_text(json.dumps(reports, indent=2) + "\n")
    recipe = dict(plan["stages"][0]["recipes"][0], operations=eligible,
                  recipe_id="p23-pi-backend-proven-acyclic-guard-promotions")
    safe_plan = {"stages": [{"recipes": [recipe], "architecture_guards": []}]}
    (ARTIFACTS / "pi-backend-precut.safe.plan.json").write_text(json.dumps(safe_plan, indent=2) + "\n")
    simulation = CodemodPlanRoot.from_json_value(safe_plan).as_sequence().simulate(snapshot)
    (ARTIFACTS / "pi-backend-precut.safe.simulation.json").write_text(json.dumps(simulation.execution_payload(), indent=2) + "\n")
    print(f"Native safe-slice execution report: {ARTIFACTS / 'pi-backend-precut.safe.simulation.json'}", flush=True)


if __name__ == "__main__":
    main()
