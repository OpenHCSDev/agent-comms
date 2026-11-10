"""Apply a retained NRA plan to source/test roots without running unrelated detectors."""

import argparse
import json
from pathlib import Path

from nominal_refactor_advisor.ast_tools import parse_python_module_roots
from nominal_refactor_advisor.codemod_preflight import CodemodOperationPreflightError
from nominal_refactor_advisor.codemod import CodemodPlanRoot, CodemodSourceSnapshot
from nominal_refactor_advisor.json_reports import json_report_object
from nominal_refactor_advisor.scan_cache import ScanCache


@ScanCache.scope()
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path)
    parser.add_argument("roots", type=Path, nargs="+")
    parser.add_argument("--simulate-only", action="store_true")
    args = parser.parse_args()
    sequence = CodemodPlanRoot.from_json_value(json.loads(args.plan.read_text())).as_sequence()
    modules = parse_python_module_roots(tuple(root.resolve() for root in args.roots))
    snapshot = CodemodSourceSnapshot.from_modules(modules)
    try:
        simulation = sequence.simulate(snapshot)
    except CodemodOperationPreflightError as error:
        print(json.dumps(json_report_object(error.report), indent=2))
        return 1
    applied = simulation.is_clean and not args.simulate_only
    if applied:
        simulation.apply()
    print(json.dumps({"applied": applied, **simulation.execution_payload()}, indent=2))
    return 0 if simulation.is_clean else 1


if __name__ == "__main__":
    raise SystemExit(main())
