"""Export the proved source prefix to its product checkout, excluding the refused tail."""
from __future__ import annotations
import json
from pathlib import Path
from nominal_refactor_advisor.codemod import CodemodPlanRoot

OUT=Path(__file__).resolve().parent
PROOF=OUT/'before'
PRODUCT=Path('/home/ts/wt/comms-friction--P17')
source=OUT/'support-body.plan.json'
# The only absolute proof-root strings in this authored payload are source/destination
# paths. Decode both versions through the existing native DSL before handing it over.
original=CodemodPlanRoot.from_json_value(json.loads(source.read_text()))
assert len(original.as_sequence().documents)==13
assert all(target.file_path and Path(target.file_path).is_relative_to(PROOF)
           for target in original.referenced_source_targets())
exported=source.read_text().replace(str(PROOF),str(PRODUCT))
plan=CodemodPlanRoot.from_json_value(json.loads(exported))
assert len(plan.as_sequence().documents)==13
assert all(target.file_path and Path(target.file_path).is_relative_to(PRODUCT)
           for target in plan.referenced_source_targets())
(OUT/'handoff-prefix.plan.json').write_text(exported)
print(OUT/'handoff-prefix.plan.json',flush=True)
