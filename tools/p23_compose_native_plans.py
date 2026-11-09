"""Compose retained native plans through NRA's declared sequence boundary."""

import argparse
import json
from pathlib import Path

from nominal_refactor_advisor.codemod import CodemodPlanRoot, CodemodPlanSequence
from nominal_refactor_advisor.json_reports import json_report_object
from nominal_refactor_advisor.scan_cache import ScanCache


@ScanCache.scope()
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("plans", type=Path, nargs="+")
    args = parser.parse_args()
    sequence = CodemodPlanSequence.compose(
        CodemodPlanRoot.from_json_value(json.loads(path.read_text()))
        for path in args.plans
    )
    args.output.write_text(json.dumps(json_report_object(sequence), indent=2) + "\n")


if __name__ == "__main__":
    main()
