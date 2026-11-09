"""Move the original low ingress contracts, without replacement records or callbacks."""

import argparse
import json
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--source-module', default='input_ingress.py',
                    help='Use execution/input_ingress.py after the owning package move.')
args = parser.parse_args()
root = Path('/home/ts/wt/comms-friction--P17/src/agent_comms')
source = str(root / args.source_module)
reason = (
    'InputRootAuthority is the original namespace/root contract inherited by actual '
    'RegistryPrivacy; InputTextContext is the independent original input lookup '
    'construction contract inherited by Comms. Neither reads TaskOwner, TaskRegistry, '
    'Checkout, UserThreadLookup or the acquisition interfaces. Their current shared '
    'module imports all of those eagerly, making low publication readers and private '
    'root authority depend on task/context assembly. Move the original declarations '
    'and all consumers through native relocation, keeping fields, slots, methods, '
    'actual provider inheritance and original binding identities. Native relocation '
    'already removes imports read only by the moved contracts; add no cleanup stage. '
    'No acquisition body changes: both snapshot_with_authority calls and every '
    'intervening root/claim recheck stay exactly where they are. No forwarding '
    'facade, surrogate record, Protocol, guard/local-import lift or custom engine work. '
    'This is an ingress declaration cut for the shared component batch, not runtime '
    'qualification. The two namespace repairs are already carried by 622c26d425 '
    'and are deliberately excluded. If the 66-module placement has applied, regenerate '
    'with --source-module execution/input_ingress.py; new destinations are already packaged.'
)
operations = [
    ('p23-isolate-original-input-root-authority', {
        'operation': 'relocate_symbols_to_new_module', 'file_path': source,
        'symbol_qualnames': ['InputRootAuthority'],
        'destination_path': str(root / 'execution/input_root_authority.py'),
    }),
    ('p23-isolate-original-input-text-context', {
        'operation': 'relocate_symbols_to_new_module', 'file_path': source,
        'symbol_qualnames': ['InputTextContext'],
        'destination_path': str(root / 'execution/input_text_context.py'),
    }),
]
plan = {'stages': [
    {'recipes': [{'recipe_id': identity, 'reason': reason, 'operations': [operation],
                  'architecture_guards': [], 'authority_claims': []}],
     'architecture_guards': []}
    for identity, operation in operations
]}
print(json.dumps(plan, indent=2))
