"""Full raw finding projection from NRA's existing complete compact API."""

import json
from dataclasses import asdict
from pathlib import Path

from nominal_refactor_advisor.analysis import AnalysisPathScope, analyze_compact_roots_with_cache
from nominal_refactor_advisor.ast_tools import PythonSourcePathPolicy
from nominal_refactor_advisor.cli import JsonScanStatus
from nominal_refactor_advisor.json_reports import json_report_object


def main():
    manifest = json.loads(Path(__file__).with_name("inventory.json").read_text())
    roots = tuple(Path(source["root"]) for source in manifest["source_context"])
    scope = AnalysisPathScope.from_requested_roots((roots[0],), roots)
    result = analyze_compact_roots_with_cache(
        scope.analysis_roots,
        use_parse_cache=False,
        parse_workers=1,
        source_policy=PythonSourcePathPolicy(include_tests=False),
        report_scope=scope,
    )
    registry = result.cache_identity.detector_registry
    print(
        json.dumps(
            {
                "scan_status": json_report_object(
                    JsonScanStatus.exact_compact_global(len(registry.detector_types))
                ),
                "detector_registry": asdict(registry),
                "source_files": [asdict(source) for source in result.cache_identity.source_files],
                "preparation_seconds": result.preparation_seconds,
                "analysis_seconds": result.analysis_seconds,
                "projection_count": result.projection_count,
                "report_roots": [str(root) for root in scope.report_roots],
                "findings": [json_report_object(finding) for finding in result.findings],
                "proof_limit": (
                    "NRA requested-roster completeness, not domain or deletion certification"
                ),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
