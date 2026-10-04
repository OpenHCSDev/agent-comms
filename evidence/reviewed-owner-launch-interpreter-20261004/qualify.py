import hashlib,importlib.util,json,sys
from pathlib import Path
OWN=Path('/home/ts/wt/comms-cleanup-live-integration-20260929')
OP=Path('/home/ts/wt/toad-prompt-action-owner-20261002/.artifacts/native-ingress638640633-context417-receiving-20261004/new-publication436')
sys.path.insert(0,str(OWN/'tools/cutover'))
from agent_comms.field_codec import FieldCodec
from agent_comms.comms import Comms
from agent_comms.owner_launch import RetainedOwnerLaunch
from agent_comms.owner_restart import OwnerRestartRequest
from publish_retained_summary import ReviewedRetainedSummaryCohort, ReviewedArtifact
from publish_openhcs_recovery import ROOT
ready_path=OP/'ready-receipt.json'
ready_sha=hashlib.sha256(ready_path.read_bytes()).hexdigest()
ready=json.loads(ready_path.read_text())
def require_frozen():
    assert hashlib.sha256(ready_path.read_bytes()).hexdigest()==ready_sha
    for path,sha in ready['artifacts'].items():
        assert hashlib.sha256(Path(path).read_bytes()).hexdigest()==sha,path
require_frozen()
spec=importlib.util.spec_from_file_location('original436_reviewed_cohort',OP/'operator-freeze/publish_retained_summary.py')
original=importlib.util.module_from_spec(spec);sys.modules[spec.name]=original;spec.loader.exec_module(original)
raw=json.loads((OP/'review-plan.json').read_text())
frozen=FieldCodec.decode(original.ReviewedRetainedSummaryCohort,raw)
cohort=ReviewedRetainedSummaryCohort(frozen.target,frozen.current_prefix,frozen.original_route,frozen.native,FieldCodec.decode(ReviewedArtifact,FieldCodec.encode(frozen.activation)),FieldCodec.decode(ReviewedArtifact,FieldCodec.encode(frozen.source_proof)),FieldCodec.decode(tuple[ReviewedArtifact,...],FieldCodec.encode(frozen.actual_gates)))
encoded=FieldCodec.encode(cohort)
assert 'source_interpreter' not in encoded
assert FieldCodec.decode(ReviewedRetainedSummaryCohort,encoded)==cohort
try:
    FieldCodec.decode(ReviewedRetainedSummaryCohort,raw)
except ValueError as error:
    refusal=str(error);assert 'source_interpreter' in refusal
else:
    raise AssertionError('Deleted-field wire plan accepted')
cohort.require_original()
service=Comms(ROOT,private_initial_writes=False,private_claim_writes=False)
snapshot=service.registry.snapshot()
threads=OwnerRestartRequest().threads(snapshot);assert threads
observed=[]
for thread in threads:
    launch=RetainedOwnerLaunch.capture(thread,snapshot)
    assert launch.interpreter and launch.process==thread.require_process()
    observed.append(launch.process.pid)
require_frozen()
assert not (OP/'publication-receipt.json').exists()
result={'state':'Declared codec and original owner read PASS','checks':['changed reviewed-cohort typed roundtrip','deleted field refused by original strict FieldCodec','unchanged typed target/source/native/original-default admission','actual original retained owner interpreter acquisition'], 'owner_count':len(observed),'frozen_artifacts_unchanged':len(ready['artifacts']),'ready_sha256':ready_sha,'frozen436_old_plan_remains_original':True,'unknown_field_refusal':refusal,'public_actions':0,'owner_restarts':0,'providers':0,'inputs':0,'package_writes':0,'scope':'Tool-boundary/actual original-owner read; no new public cutover or UI claim'}
(OWN/'evidence/reviewed-owner-launch-interpreter-20261004/qualification.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(result))
