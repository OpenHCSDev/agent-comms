"""One final real-file batch for the installed writer and granted source owner."""
from pathlib import Path
import hashlib,json,stat,sys
prefix=Path('/home/ts/wt/comms-task-aware-native-bundle-20261002/.artifacts/runtime-summary-generation-policy520-installed-20261002')
assert Path(sys.prefix)==prefix
from agent_comms import store_files
assert Path(store_files.__file__).is_relative_to(prefix)
sys.path.insert(0,'/home/ts/wt/comms-task-aware-native-bundle-20261002/tools/cutover')
sys.path.append('/home/ts/wt/comms-cleanup-live-integration-20260929/tools/cutover')
import importlib.util
publisher_path=Path('/home/ts/wt/comms-task-aware-native-bundle-20261002/tools/cutover/publish_retained_summary.py')
# Ensure the original target publisher, rather than the tracking checkout version.
spec=importlib.util.spec_from_file_location('publish_retained_summary',publisher_path)
publisher=importlib.util.module_from_spec(spec);sys.modules[spec.name]=publisher;spec.loader.exec_module(publisher)
from publish_retained_summary import ReviewedArtifact,PublishRetainedSummary,ReviewedRetainedSummaryCohort
from runtime_installation import PreserveRuntimeInstallation
from agent_comms.field_codec import FieldCodec
from agent_comms.private_path import PrivateFileRole
from global_extension_activation import ReplaceGlobalSource,CreateGlobalSource,ActivateGlobalExtension
out=Path(__file__).resolve().parent
root=out/'final552-installed-actual-files';root.mkdir(mode=0o700,exist_ok=False)
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
source=root/'reviewed-source';source.write_text('#!/bin/sh\nexit 0\n')
approved=ReviewedArtifact(source,sha(source))
results=[]
for label,mode in [('executable',0o755),('ordinary',0o644)]:
 destination=root/label;destination.write_text('original '+label+'\n');destination.chmod(mode)
 original=destination.read_bytes()
 member=ReplaceGlobalSource(approved,destination,ReviewedArtifact(destination,sha(destination)))
 preimages=root/(label+'-preimages');preimages.mkdir(mode=0o700)
 member.retain_original(preimages);member.install()
 assert destination.read_bytes()==source.read_bytes()
 assert stat.S_IMODE(destination.stat().st_mode)==mode
 assert (preimages/destination.name).read_bytes()==original
 results.append({'member':label,'mode':oct(mode),'new_bytes_equal':True,'preimage_preserved':True})
 # The original reviewed artifact no longer matches the replaced source.
 try:member.install()
 except RuntimeError:pass
 else:raise AssertionError('Changed original was admitted')
 assert destination.read_bytes()==source.read_bytes()
private=root/'private';store_files._atomic_write_text(private,'private original-default\n',fsync_parent=True)
assert stat.S_IMODE(private.stat().st_mode)==0o600
created=root/'created';CreateGlobalSource(approved,created).install()
assert created.read_bytes()==source.read_bytes() and stat.S_IMODE(created.stat().st_mode)==0o600
# One actual physical witness: unchanged originals qualify, changed source
# digest or newly created destination refuses source-runtime restoration.
public_operand=Path('/home/ts/wt/comms-task-aware-native-bundle-20261002/.artifacts/client-custody548-context547309-20261002/new-publication548343/review-plan-pending-frontend.json')
cohort=FieldCodec.decode(ReviewedRetainedSummaryCohort,json.loads(public_operand.read_text()))
private_root=root/'private-root';private_root.mkdir(mode=0o700)
publisher.ROOT=private_root
for label,new in [('replaced',False),('created',True)]:
 destination=root/('witness-'+label)
 if new:member=CreateGlobalSource(approved,destination)
 else:
  destination.write_text('original witness\n');destination.chmod(0o755)
  member=ReplaceGlobalSource(approved,destination,ReviewedArtifact(destination,sha(destination)))
 operation=ActivateGlobalExtension((member,),root/(label+'-activation-preimages'))
 assert operation.recovery_paths()==frozenset((destination,))
 publication=PublishRetainedSummary(cohort,(),(),operation,PreserveRuntimeInstallation({}),-1,root/(label+'-receipt.json'))
 publication.require_recovery_originals()
 assert (destination in frozenset(item.path for item in publication.recovery_originals)) is (not new)
 member.install()
 try:publication.require_recovery_originals()
 except RuntimeError as exc:refusal=str(exc)
 else:raise AssertionError('Source change admitted original-runtime restoration')
 assert destination.read_bytes()==source.read_bytes()
 results.append({'member':label,'source_witness':True,'unchanged_accepted':True,'changed_refused':refusal,'accepted_source_bytes_remain':True})
assert not tuple(root.glob('.*.tmp'))
receipt={'state':'final-installed-portable-mode-and-source-recovery-witness-passed','prefix':str(prefix),'installed_writer':str(store_files.__file__),'installed_writer_sha256':sha(Path(store_files.__file__)),'global_source_owner':'existing parent-tracked GlobalSourceInstall/ReplaceGlobalSource; no new installer/atomic writer','global_source_owner_sha256':sha(Path('/home/ts/wt/comms-cleanup-live-integration-20260929/tools/cutover/global_extension_activation.py')),'cases':results,'private_default_mode':'0o600','new_global_source_mode':'0o600','changed_original_refused':True,'temporary_files_remaining':False,'public_changes':0,'provider_calls':0,'scope':'Installed final552 actual filesystem primitive and existing reviewed source behavior; no public deployment, provider or repeated UI/lifetime journey'}
(out/'final552-installed-file-mode-recovery-receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
print(json.dumps(receipt,indent=2))
