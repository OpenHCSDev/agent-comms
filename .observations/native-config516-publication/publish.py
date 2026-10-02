"""Parent-only invocation of the existing Native6 stopped-batch publisher."""
from pathlib import Path
import argparse
import hashlib
import json
import sys

OPERATOR = Path('/home/ts/wt/comms-native-compaction-source-carry-20261002')
MANIFEST = OPERATOR / 'evidence/native-compaction-source-carry-20261002/current9ccc-to-final28401/operator-tools-manifest.json'

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('cohort', type=Path)
parser.add_argument('installation', type=Path)
parser.add_argument('receipt', type=Path)
parser.add_argument('--execute', action='store_true')
args = parser.parse_args()

# The original reviewed operator manifest owns the complete source dependency.
if hashlib.sha256(MANIFEST.read_bytes()).hexdigest() != '506bd52e5602fe564ff42b80ca5e19105af712d8aa6f0ce8253848852800ef22':
    raise RuntimeError('Reviewed operator manifest changed')
manifest = json.loads(MANIFEST.read_text())
for name, checksum in manifest['files'].items():
    if hashlib.sha256((OPERATOR / name).read_bytes()).hexdigest() != checksum:
        raise RuntimeError('Reviewed operator changed: ' + name)
sys.path.insert(0, str(OPERATOR / 'tools/cutover'))
from publish_retained_summary import ReviewedRetainedSummaryCohort, publish
from runtime_installation import RuntimeInstallation
from agent_comms.field_codec import FieldCodec
from agent_comms.owner_cutover import PreserveOwnerRuntime

cohort = FieldCodec.decode(ReviewedRetainedSummaryCohort, json.loads(args.cohort.read_text()))
installation = FieldCodec.decode(RuntimeInstallation, json.loads(args.installation.read_text()))
cohort.require_original()
if args.execute:
    publish(cohort, PreserveOwnerRuntime(), installation, args.receipt)
else:
    print('Reviewed artifacts decoded; no source capture, fence, stop, install or publication executed.')
