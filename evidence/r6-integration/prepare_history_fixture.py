"""Copy history metadata for R6 migration/UI acceptance; never migrate live routes."""
from pathlib import Path
from contextlib import closing
import json, shutil, sqlite3
from agent_comms.store_files import file_revision

source=Path('/var/tmp/agent-comms-live-20260927-wzjtqhza')
target=Path('/home/ts/wt/comms-c0-integration-20260928/.artifacts/r6-history-fixture')
def copy_metadata(original, destination):
    destination.mkdir(mode=0o700,parents=True,exist_ok=False)
    for path in original.iterdir():
        if path.is_file() and path.name != 'bus_meta.json' and (path.suffix == '.json' or path.name in ('bus.jsonl','activity.jsonl')):
            shutil.copy2(path,destination/path.name)
    for name in ('transcript_routes.sqlite3','transcript_reply_index.sqlite3'):
        db=original/name
        if not db.exists(): continue
        with closing(sqlite3.connect(db.resolve().as_uri()+'?mode=ro',uri=True)) as old, closing(sqlite3.connect(destination/db.name)) as new:
            old.backup(new)
# Display-only copy: no transplanted private writer/checkpoint authority.
copy_metadata(source,target)
manifest=json.loads((target/'history_sources.json').read_text())
for item in manifest:
    original=Path(item['root'])
    destination=target/'history'/original.name
    copy_metadata(original,destination)
    item['root']=str(destination)
    item['snapshot_bus_revision']=file_revision(destination/'bus.jsonl')
    item['snapshot_registry_revision']=file_revision(destination/'registry.json')
(target/'history_sources.json').write_text(json.dumps(manifest))
print(json.dumps({'fixture':str(target),'historical_sources':len(manifest),'originals_mutated':False}))
