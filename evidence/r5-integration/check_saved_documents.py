"""Observe old/new metadata and collaboration documents on owned saved-data copies."""
import json
import sys
from dataclasses import asdict
from pathlib import Path

from agent_comms.activity import ActivityLog
from agent_comms.runtime_info import RuntimeInfoStore
from agent_comms.shared_ledger import SharedLedger

new = sys.argv[1] == 'new'
base = Path(sys.argv[2])
result = {}
for root in sorted(base.iterdir()):
    if not root.is_dir():
        continue
    store = RuntimeInfoStore(root / 'runtime_info.json')
    info = store.read() if new else store.all()
    ledger = SharedLedger(root / 'ledger.json').read()
    activity = ActivityLog(root / 'activity.jsonl')._latest_events()
    result[root.name] = {'runtime': {key:asdict(value) for key,value in info.items()},
                         'ledger': ledger,
                         'activity': {key:asdict(value) for key,value in activity.items()}}
print(json.dumps(result, sort_keys=True, default=lambda value:value.value))
