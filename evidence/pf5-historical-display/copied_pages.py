"""Compare main208 and PF5 on owned copies of preserved historical buses."""
from pathlib import Path
from dataclasses import asdict
from contextlib import closing
import json
import sqlite3
import shutil
import sys

from agent_comms.comms import Comms
from agent_comms.historical_views import HistoryCursor
from agent_comms.store_files import file_revision

OWN = Path(__file__).resolve().parents[2]
ROOT = OWN / '.artifacts/original-history'
RECEIPT = OWN / 'evidence/pf5-historical-display'

def prepare():
    source = Path('/var/tmp/agent-comms-live-20260927-wzjtqhza')
    def copy(original, dest):
        dest.mkdir(parents=True, mode=0o700)
        for path in original.iterdir():
            if path.is_file() and path.name != 'bus_meta.json' and (path.suffix == '.json' or path.name == 'bus.jsonl'):
                shutil.copy2(path, dest / path.name)
    copy(source, ROOT)
    manifest = json.loads((ROOT / 'history_sources.json').read_text())
    for i, row in enumerate(manifest):
        dest = ROOT / 'history' / str(i)
        copy(Path(row['root']), dest)
        row['root'] = str(dest)
        row['snapshot_bus_revision'] = file_revision(dest / 'bus.jsonl')
        row['snapshot_registry_revision'] = file_revision(dest / 'registry.json')
    (ROOT / 'history_sources.json').write_text(json.dumps(manifest))
    # Preserve the disposable native unread cache for mounted channel-only UI.
    index = source / 'transcript_reply_index.sqlite3'
    if index.exists():
        with closing(sqlite3.connect(index.as_uri() + '?mode=ro', uri=True)) as old:
            with closing(sqlite3.connect(ROOT / index.name)) as new:
                old.backup(new)
    return len(manifest)

def fact(page):
    def cursor(value):
        return asdict(value) if isinstance(value, HistoryCursor) else value
    return {
        'rows': [dict(wire=m.to_wire(), key=m.view_key, metadata=m.display_metadata) for m in page.messages],
        'older': page.has_older, 'newer': page.has_newer,
        'oldest': cursor(page.oldest_cursor), 'newest': cursor(page.newest_cursor),
    }

def capture():
    comms = Comms(ROOT)
    cases = []
    for target in ('#comms', '#nra', '#all', '#any', '#none', 'broadcast'):
        for limit, budget in ((8, 128000), (2, 256)):
            reader = comms.views.channel_history_page
            page = reader(target, limit=limit, max_bytes=budget)
            cases.append((f'{target}:{limit}:tail', fact(page)))
            if page.messages:
                cases.append((f'{target}:{limit}:before', fact(reader(target, before=page.oldest_cursor, limit=limit, max_bytes=budget))))
                cases.append((f'{target}:{limit}:after', fact(reader(target, after=page.oldest_cursor, limit=limit, max_bytes=budget))))
    for target in ('#comms', '#nra', '#any'):
        page = comms.views.channel_display_page(target, limit=8)
        cases.append((f'display:{target}', fact(page)))
    # A preserved source pair exercises its original aliases/names; no live participant registration.
    source = comms.bus.history_sources()[0]
    rows = [json.loads(line) for line in (Path(source.root) / 'bus.jsonl').read_text().splitlines() if line.strip()]
    pair = next((r['from'], r['to']) for r in reversed(rows) if not r['to'].startswith('#') and r['to'] != 'broadcast')
    for limit, budget in ((8, 128000), (2, 256)):
        page = comms.views.dm_history_page(*pair, before=HistoryCursor(source.key, 10**12), limit=limit, max_bytes=budget)
        cases.append((f'dm:{pair}:{limit}', fact(page)))
    return cases

mode = sys.argv[1]
if mode == 'baseline':
    print('copied_sources', prepare(), flush=True)
    rows = capture()
    (OWN / '.artifacts/original-baseline.json').write_text(json.dumps(rows, sort_keys=True))
    print('baseline_cases', len(rows), flush=True)
else:
    baseline = json.loads((OWN / '.artifacts/original-baseline.json').read_text())
    # A cold index validates every row; warm reads must return the same public page.
    for path in ROOT.rglob('bus_page_index.sqlite3'):
        path.unlink()
    cold = json.loads(json.dumps(capture()))
    warm = json.loads(json.dumps(capture()))
    assert cold == baseline
    assert warm == baseline
    print(json.dumps({'cases': len(cold), 'cold_equals_main208': True, 'warm_equals_main208': True, 'originals_read_only': True}), flush=True)
