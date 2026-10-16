"""Existing reviewed artifact relation; no cutover/public admission."""
from pathlib import Path
import argparse
import json,sys
from dataclasses import replace
sys.path.insert(0,str(Path(__file__).resolve().parent))
from publish_retained_summary import (
 CohortActivation,InstalledSourceProof,ArchivePackageDirectUrl,VcsPackageDirectUrl,
 PackageVcsInfo,
)
from agent_comms.field_codec import FieldCodec
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--activation',type=Path,required=True)
parser.add_argument('--source-proof',type=Path,required=True)
parser.add_argument('--output',type=Path,required=True)
args=parser.parse_args()
a_path=args.activation
p_path=args.source_proof
a=FieldCodec.decode(CohortActivation,json.loads(a_path.read_text()))
p=FieldCodec.decode(InstalledSourceProof,json.loads(p_path.read_text()))
p.require_activation(a)
checks=['original four-source proof matches three activation pins plus declared Diff scalar']
for name,proof in (
 ('missing-diff', replace(p,sources=tuple(s for s in p.sources if s.module!='textual_diff_view'))),
 ('changed-diff', replace(p,sources=tuple(replace(s,head='0'*40) if s.module=='textual_diff_view' else s for s in p.sources))),
 ('duplicate-module',replace(p,sources=p.sources+(p.sources[-1],))),
 ('source-overlay',replace(p,source_overlay=True)),
 ('wrong-native',replace(p,native_tree='0'*64)),
):
 try: proof.require_activation(a)
 except RuntimeError: checks.append(name+' refused')
 else: raise AssertionError(name+' accepted')
for artifact in p.archive_artifacts:
 url=artifact.path.as_uri()
 for info in ({}, {'hash':f'sha256={artifact.sha256}'},
              {'hashes':{'sha256':artifact.sha256}},
              {'hash':f'sha256={artifact.sha256}','hashes':{'sha256':artifact.sha256}}):
  origin=FieldCodec.decode(ArchivePackageDirectUrl,{'url':url,'archive_info':info})
  origin.require_original('source head is separately verified', (artifact,))
 checks.append('original local wheel accepts empty, legacy, modern and consistent combined archive_info')
 for name,info,witnesses in (
  ('missing-wheel-witness',{},()),
  ('duplicate-wheel-witness',{},(artifact,artifact)),
  ('changed-wheel-hash',{},(replace(artifact,sha256='0'*64),)),
  ('different-archive-hash',{'hashes':{'sha256':'0'*64}},(artifact,)),
  ('conflicting-archive-hashes',{'hash':f'sha256={artifact.sha256}','hashes':{'sha256':'0'*64}},(artifact,)),
  ('malformed-legacy-hash',{'hash':'not-a-hash'},(artifact,)),
 ):
  origin=FieldCodec.decode(ArchivePackageDirectUrl,{'url':url,'archive_info':info})
  try: origin.require_original('source head is separately verified',witnesses)
  except RuntimeError: checks.append(name+' refused')
  else: raise AssertionError(name+' accepted')
 for payload in (
  {'url':url,'archive_info':{},'vcs_info':{'vcs':'git','commit_id':'0'*40,'requested_revision':'0'*40}},
  {'url':url,'archive_info':{'unknown_hash_field':artifact.sha256}},
 ):
  try: FieldCodec.decode(VcsPackageDirectUrl|ArchivePackageDirectUrl,payload)
  except ValueError: checks.append('ambiguous/unknown origin fields refused by original FieldCodec')
  else: raise AssertionError('ambiguous/unknown origin accepted')
origin=VcsPackageDirectUrl('https://example.invalid/source',PackageVcsInfo('git','0'*40,'0'*40))
try: origin.require_original('1'*40,())
except RuntimeError: checks.append('wrong VCS source head refused by origin owner')
else: raise AssertionError('wrong VCS source accepted')
import hashlib
receipt={'state':'existing-cohort-artifact-boundary-passed','checks':checks,
 'activation_sha256':hashlib.sha256(a_path.read_bytes()).hexdigest(),
 'source_proof_sha256':hashlib.sha256(p_path.read_bytes()).hexdigest(),
 'source_heads':a.source_heads(),'public_actions':0,'providers':0,
 'strength':'original immutable package relationship; not admission or actual future gate'}
args.output.write_text(json.dumps(receipt,indent=2)+'\n')
print(json.dumps(receipt,indent=2))
