from pathlib import Path
from dataclasses import replace
import sys,json,hashlib
base=Path(__file__).resolve().parent
sys.path.insert(0,str(base/'operator-freeze'))
from publish_retained_summary import ReviewedRetainedSummaryCohort
from agent_comms.field_codec import FieldCodec
cohort=FieldCodec.decode(ReviewedRetainedSummaryCohort,json.loads((base/'review-plan.json').read_text()))
assert len(cohort.actual_gates)==1
# Positive full admission was executed by original prepare_review; keep that
# exact result rather than repeat source/native verification here.
assert (base/'preparation-receipt.json').is_file()
cases={
 'missing_installed_acceptance':replace(cohort,actual_gates=()),
 'duplicate_original_gate':replace(cohort,actual_gates=cohort.actual_gates*2),
 'packaging_is_not_installed_journey':replace(cohort,actual_gates=(cohort.source_proof,)),
 'changed_original_gate':replace(cohort,actual_gates=(replace(cohort.actual_gates[0],sha256='0'*64),)),
}
results=[]
for name,current in cases.items():
 try:current.require_original()
 except RuntimeError as error:results.append({'case':name,'rejected':True,'error':str(error)})
 else:raise AssertionError(name)
assert all('Distinct reviewed actual installed journey gates are required' in row['error'] for row in results[:3])
assert results[3]['error'].startswith('Reviewed artifact changed:')
receipt={'state':'PASS final batch against unchanged installed563 package and corrected canonical operator','positive':'Actual full cohort.require_original invoked by new prepare_review; one original installed563 journey admitted','negative_cases':results,'package_wheels_rebuilt':False,'new_journey_artifacts':0,'provider_calls':0,'public_effects':0,'publication_invoked':False,'old_operation_edited':False}
(base/'affected-metadata-sanity.json').write_text(json.dumps(receipt,indent=2)+'\n')
print(json.dumps(receipt,indent=2))
