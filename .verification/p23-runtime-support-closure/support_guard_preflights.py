"""Native guard evidence on the retained body cut, before the refused vocabulary cut."""
from __future__ import annotations
import json
from pathlib import Path
from nominal_refactor_advisor.codemod import CodemodSourceSnapshot
from nominal_refactor_advisor.json_reports import json_report_object
from author_support import guarded_plan

OUT=Path(__file__).resolve().parent
ROOT=OUT/'projected'
sources={str(file):file.read_text() for file in ROOT.rglob('*.py')}
snapshot=CodemodSourceSnapshot.from_source_mapping(sources,analysis_roots=(ROOT/'src',))
report=guarded_plan(snapshot,ROOT).preflight(snapshot).report
(OUT/'support-imports.precut.preflight.json').write_text(json.dumps(json_report_object(report),indent=2)+'\n')
print(json.dumps(json_report_object(report),indent=2),flush=True)
print('PREFLIGHT ONLY; original vocabulary check still present; no simulate/apply',flush=True)
