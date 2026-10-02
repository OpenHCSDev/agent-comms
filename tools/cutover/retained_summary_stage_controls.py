"""Existing reviewed artifact relation; no cutover/public admission."""
from pathlib import Path
import argparse
import json,sys
from dataclasses import replace
sys.path.insert(0,str(Path(__file__).resolve().parent))
from publish_retained_summary import CohortActivation,InstalledSourceProof
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
import hashlib
receipt={'state':'existing-cohort-artifact-boundary-passed','checks':checks,
 'activation_sha256':hashlib.sha256(a_path.read_bytes()).hexdigest(),
 'source_proof_sha256':hashlib.sha256(p_path.read_bytes()).hexdigest(),
 'source_heads':a.source_heads(),'public_actions':0,'providers':0,
 'strength':'original immutable package relationship; not admission or actual future gate'}
args.output.write_text(json.dumps(receipt,indent=2)+'\n')
print(json.dumps(receipt,indent=2))
