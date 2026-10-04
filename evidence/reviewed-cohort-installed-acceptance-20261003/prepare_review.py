"""Prepare reviewed original writer/cohort operands without public effects."""
from pathlib import Path
import hashlib
import json
import os
import subprocess
import sys
from agent_comms.private_path import PrivateDirectoryRole
from agent_comms.field_codec import FieldCodec
from agent_comms.active_route import read_active_route
base=Path(__file__).resolve().parent
PrivateDirectoryRole.require(base.lstat())
operator=base/'operator-freeze'
sys.path.insert(0,str(operator))
from native_schema_carry import NativeSchemaDeclaration
from runtime_installation import PreserveRuntimeInstallation
from publish_retained_summary import ReviewedArtifact,ReviewedRetainedSummaryCohort
out=base.parent
owner=json.loads((out/'ownership.json').read_text())
target=Path(owner['prefix'])
source=Path('/home/ts/wt/toad-prompt-action-owner-20261002/.artifacts/runtime-summary529-geometry322-20261002')
assert sys.executable==str(target/'bin/python')
def digest(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def artifact(p):return ReviewedArtifact(p,digest(p))
def save(name,value):
 p=base/name;p.write_text(json.dumps(value,indent=2,allow_nan=False)+'\n');return p
assert not (base/'publication-receipt.json').exists()
assert not (base/'publication-receipt.originals').exists()
env=dict(os.environ);env.pop('PYTHONPATH',None);env.pop('PYTHONHOME',None)
program="import sys,json; sys.path.insert(0,sys.argv[1]); from native_schema_carry import NativeSchemaDeclaration; from agent_comms.field_codec import FieldCodec; print(json.dumps(FieldCodec.encode(NativeSchemaDeclaration.observe())))"
original=FieldCodec.decode(NativeSchemaDeclaration,json.loads(subprocess.check_output([str(source/'bin/python'),'-I','-c',program,str(operator)],env=env,text=True)))
current=NativeSchemaDeclaration.observe()
assert original==current,'Native declarations changed; preserve-only not qualified'
old_index=subprocess.check_output([str(source/'bin/python'),'-I',str(operator/'checkpoint_schema.py')],env=env,text=True).strip()
new_index=subprocess.check_output([str(target/'bin/python'),'-I',str(operator/'checkpoint_schema.py')],env=env,text=True).strip()
assert old_index==new_index,'Derived index declaration changed; preserve-only not qualified'
gates=tuple(artifact(out/'qualified-source-gates'/name) for name in owner['gate_files'])
cohort=ReviewedRetainedSummaryCohort(target=target,source_interpreter=source/'bin/python',current_prefix=source,original_route=read_active_route(),native=Path(owner['native']),activation=artifact(target/'activation.json'),source_proof=artifact(out/'source-proof.json'),actual_gates=gates)
cohort.require_original()
save('original-declaration.json',FieldCodec.encode(original))
save('target-declaration.json',FieldCodec.encode(current))
save('review-plan.json',FieldCodec.encode(cohort))
runtime=save('runtime-installation.json',FieldCodec.encode(PreserveRuntimeInstallation(original.goal)))
save('declaration-relation.json',{'whole_declared_native_schema_equal':True,'product_equal':False,'source_core':'85abe4276e91d52a72eec7411ed2314aaccc8c02','target_core':owner['core'],'original_checkpoint_schema':old_index,'target_checkpoint_schema':new_index,'changed_derived_checkpoint':False,'runtime_carry_reset_required':False,'runtime_member':'PreserveRuntimeInstallation','owner_member':'PreserveOwnerRuntime','original_stores_opened':False,'native_unchanged':True,'source_native_manifest':'de16647979cbad28','target_native_manifest':'de16647979cbad28'})
receipt=save('preparation-receipt.json',{'state':'FROZEN installed563 qualified certified read-worker/turn-publication custody; parent-only NEW preserve publication','target_prefix':str(target),'source_prefix':str(source),'native_schema_equal':True,'derived_checkpoint_schema_equal':True,'native':owner['native'],'operator_manifest':str(base/'operator-manifest.json'),'operator_manifest_sha256':digest(base/'operator-manifest.json'),'source_proof_sha256':cohort.source_proof.sha256,'activation_sha256':cohort.activation.sha256,'runtime_sha256':digest(runtime),'public_effects':0,'new_publication_receipt':str(base/'publication-receipt.json'),'unchanged_journeys_repeated':False,'parent_public_execution':True})

print(receipt.read_text())
