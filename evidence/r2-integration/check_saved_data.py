"""Compare old/new logical catalog and message projections on private saved-data copies."""
import hashlib
import json
import sys
from pathlib import Path

from agent_comms.field_codec import FieldCodec
from agent_comms.messages import Message
from agent_comms.registration import Registration

new = sys.argv[1] == 'new'
base = Path(sys.argv[2])
if new:
    from agent_comms.catalog_store import ChannelCatalog
else:
    from agent_comms.channels import ChannelCatalog

report = {}
for root in sorted(base.iterdir()):
    if not root.is_dir():
        continue
    registry = Registration(root / 'registry.json')
    if new:
        catalog = ChannelCatalog(root / ChannelCatalog.filename)
        document = catalog.read()
        original = FieldCodec.encode(document)
        with catalog.editing():
            pass
        assert FieldCodec.encode(catalog.read()) == original
        views = document.views(registry.all_threads())
        saved = document.saved_views
        pins = document.pinned_members()
        order = document.list_order
    else:
        catalog = ChannelCatalog(root / 'channels.json', registry)
        views = catalog.views()
        saved = catalog.saved_views()
        pins = catalog.pinned_threads_snapshot()
        order = catalog.list_order
    projection = {'views': FieldCodec.encode(views), 'saved': FieldCodec.encode(saved),
                  'pins': FieldCodec.encode(pins), 'order': order.value}
    digest = hashlib.sha256()
    failures = []
    count = 0
    for number, line in enumerate((root / 'bus.jsonl').read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            message = Message.from_wire(json.loads(line))
            encoded = json.dumps(message.to_wire(), sort_keys=True, separators=(',', ':'))
            digest.update((encoded+'\n').encode())
            count += 1
        except Exception as error:
            failures.append({'row': number, 'error': type(error).__name__, 'message': str(error)})
    report[root.name] = {'catalog': projection, 'messages': count,
                         'message_projection_digest': digest.hexdigest(), 'failures': failures}
print(json.dumps(report, sort_keys=True))
