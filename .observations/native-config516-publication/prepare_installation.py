"""Parent-only quiet preparation using the original Native6 carry owner."""
from pathlib import Path
import argparse
import json
import sys

OPERATOR = Path('/home/ts/wt/comms-native-compaction-source-carry-20261002')
sys.path.insert(0, str(OPERATOR / 'tools/cutover'))
from native_schema_carry import NativeSchemaDeclaration, prepare
from runtime_installation import CarryNativeRuntimeInstallation
from routing_recovery import write_original
from agent_comms.field_codec import FieldCodec

parser = argparse.ArgumentParser(description=__doc__)
for name in ('root', 'candidate', 'original_declaration', 'source_python', 'plan', 'installation'):
    parser.add_argument(name, type=Path)
args = parser.parse_args()
original = FieldCodec.decode(NativeSchemaDeclaration, json.loads(args.original_declaration.read_text()))
plan = prepare(args.root, args.candidate, original, args.source_python)
installation = CarryNativeRuntimeInstallation(goal_schema=original.goal, plan=plan)
write_original(args.plan, (json.dumps(FieldCodec.encode(plan), indent=2)+'\n').encode())
write_original(args.installation, (json.dumps(FieldCodec.encode(installation), indent=2)+'\n').encode())
print('Quiet candidate prepared; no original file replaced, owner stopped or publication performed.')
