"""Changed tool audience boundaries against the real current typed registry."""
from pathlib import Path
import hashlib,json,os,sys
prefix=Path('/home/ts/wt/comms-task-aware-native-bundle-20261002/.artifacts/runtime-summary-generation-policy520-installed-20261002')
assert Path(sys.prefix)==prefix
sys.path.insert(0,'/home/ts/wt/comms-task-aware-native-bundle-20261002/tools/cutover')
from agent_comms.comms import Comms
from agent_comms.active_route import active_route_path
from agent_comms.field_codec import FieldCodec
from agent_comms.errors import RelationViolationError
from agent_comms.owner_restart import OwnerRestartRequest
from agent_comms.owner_lifecycle import OwnerRestartSelection
from agent_comms.owner_cutover import PreserveOwnerRuntime
from publish_retained_summary import PublishRetainedSummary,ReviewedRetainedSummaryCohort
from runtime_installation import RuntimeInstallation
from retained_index_cutover import RetainedIndexCutover
from retained_task_source_carry import RetainedTaskSourceCarry
from thread_retirement_cutover import ThreadRetirementCutover

root=Path('/var/tmp/agent-comms-live-20260927-wzjtqhza')
out=Path(__file__).resolve().parent
old=Path('/home/ts/wt/comms-task-aware-native-bundle-20261002/.artifacts/client-custody548-context547309-20261002/new-publication548343')
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
failed_before=sha(old/'publication-receipt.json')
source=Comms(root,private_initial_writes=False,private_claim_writes=False)
snapshot=source.registry.snapshot()
request=OwnerRestartRequest()
owners=tuple(request.threads(snapshot))
external=snapshot.threads['dedicated-worktree-cleanup-20261001']
assert external.execution.__name__=='ExternalThreadExecution'
assert not external.process_alive and not snapshot.statuses[external.name].active
assert external.name not in {t.name for t in owners}
assert len(owners)==19
cohort=FieldCodec.decode(ReviewedRetainedSummaryCohort,json.loads((old/'review-plan.json').read_text()))
runtime=FieldCodec.decode(RuntimeInstallation,json.loads((old/'runtime-installation.json').read_text()))
audience=tuple(OwnerRestartSelection.capture(snapshot,t.name) for t in owners)
route=os.open(active_route_path().parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
try:
 operation=PublishRetainedSummary(cohort,audience,owners,PreserveOwnerRuntime(),runtime,route,out/'never-executed.json')
 # Runs the actual read-only admission checks, not restart/fence/complete.
 operation.require_selection(snapshot,owners)
 checks=[{'case':'actual_complete19','accepted':True}]
 others=(
  ('summary',operation),
  ('index_and_inherited_routing',RetainedIndexCutover(cohort.source_interpreter,cohort.original_route.wire_root_id,cohort.native)),
  ('task_carry',RetainedTaskSourceCarry(cohort.source_interpreter,root,cohort.original_route.wire_root_id,out/'never-task.json')),
  ('thread_retirement',ThreadRetirementCutover(cohort.target/'bin/python',dict(os.environ),active_route_path(),route,FieldCodec.encode(cohort.original_route),FieldCodec.encode(cohort.original_route),out/'never-retirement.json')),
 )
 for label,member in others:
  for case,selected in [('includes_external',(*owners,external)),('missing_native',owners[:-1])]:
   try:member.require_selection(snapshot,selected)
   except (RuntimeError,RelationViolationError) as exc:reason=str(exc)
   else:raise AssertionError((label,case,'audience mismatch admitted'))
   checks.append({'member':label,'case':case,'refused':reason})
finally:os.close(route)
after=source.registry.snapshot()
for original in owners:
 current=after.threads[original.name]
 assert current.process_identity==original.process_identity and current.incarnation==original.incarnation
assert sha(old/'publication-receipt.json')==failed_before
assert not (out/'never-executed.json').exists()
result={'state':'changed-tool-readonly-admission-batch-passed','installed_library':'3c48ace44f2f8ce00c55638e23655b1ed50a3b5a unchanged','source_tools_head':'9610f42c','managed_count':len(owners),'managed_names':[t.name for t in owners],'external':{'name':external.name,'execution':external.execution.__name__,'active':snapshot.statuses[external.name].active,'process_alive':external.process_alive,'incarnation':FieldCodec.encode(external.incarnation)},'checks':checks,'original_owner_identities_unchanged':True,'failed_receipt_sha256':failed_before,'failed_receipt_unchanged':True,'fence_stop_launch_install_calls':0,'provider_inputs':0,'scope':'Real installed canonical owner request and current typed registry; changed tool admission only. No public cutover, new native/UI or fabricated participant state.'}
(out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps({'state':result['state'],'managed_count':len(owners),'cases':len(checks),'failed_receipt_unchanged':True},indent=2))
