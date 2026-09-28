"""Read original saved files only; all registry/route copies stay in owned fixtures."""
import json
import shutil
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from agent_comms.comms import wire
from agent_comms.threads import Thread
from agent_comms.thread_identity import ThreadRole
from agent_comms.transcripts import TranscriptCursor

HERE = Path(__file__).resolve().parents[2]
FIXTURE = HERE / '.artifacts/r6/saved-pages'
GOLDEN = FIXTURE / 'expected.json'


def fact(event):
    if hasattr(event, 'to_wire'):
        data = event.to_wire()
    else:
        from agent_comms.transcript_events import TranscriptCodec
        data = TranscriptCodec.encode(event)
    defaults = {'routing': None, 'text': '', 'tool_call_id': '', 'tool_name': '', 'raw_input': None, 'ok': True, 'diff': None}
    return {key: value for key, value in data.items() if key not in defaults or value != defaults[key]}


def main():
    capture = sys.argv[1] == 'capture'
    if capture:
        root = Path('/var/tmp/agent-comms-live-20260927-wzjtqhza')
        sources = [root, *[Path(row['root']) for row in json.loads((root/'history_sources.json').read_text())]]
        sessions = {}
        for source in sources:
            for row in json.loads((source/'registry.json').read_text())['threads'].values():
                if path := row.get('session_file'):
                    sessions.setdefault(path, str(source))
        fixtures = {}
        FIXTURE.mkdir(parents=True, exist_ok=True)
        for i, source in enumerate(sources):
            fixture = FIXTURE / str(i)
            fixture.mkdir(exist_ok=True)
            for filename in ['transcript_routes.json', 'transcript_routes.sqlite3']:
                src, dst = source/filename, fixture/filename
                if src.is_file():
                    if src.suffix == '.sqlite3':
                        with closing(sqlite3.connect(src.resolve().as_uri()+'?mode=ro', uri=True)) as a, closing(sqlite3.connect(dst)) as b:
                            a.backup(b)
                    else:
                        shutil.copyfile(src, dst)
            fixtures[str(source)] = str(fixture)
        expected = []
        for i, (path, source) in enumerate(sessions.items()):
            comms = wire(Path(fixtures[source]))
            name = f'audit-reader-{i}'
            comms.threads.register(Thread(name, frozenset(), fixtures[source], session_file=path, role=ThreadRole.USER, created_at=i+1.0))
            page = comms.transcripts.thread_transcript_page(name)
            pages = [page]
            if page.has_older:
                pages.append(comms.transcripts.thread_transcript_page(name,before=page.before,through=page.after))
            expected.append({'root':fixtures[source], 'name':name, 'through':page.after.offset, 'path':path,
                'pages':[{'metadata':p.metadata(), 'events':[fact(e) for e in p.events]} for p in pages]})
        GOLDEN.write_text(json.dumps(expected))
        print(json.dumps({'captured_sessions':len(expected), 'fixture_bytes':GOLDEN.stat().st_size}))
    else:
        expected=json.loads(GOLDEN.read_text())
        failures=[]
        for item in expected:
            comms=wire(Path(item['root']))
            through=TranscriptCursor(item['path'],item['through'])
            try:
                page=comms.transcripts.thread_transcript_page(item['name'],through=through)
                pages=[page]
                if len(item['pages'])==2:
                    pages.append(comms.transcripts.thread_transcript_page(item['name'],before=page.before,through=through))
                actual=[{'metadata':p.metadata(),'events':[fact(e) for e in p.events]} for p in pages]
                if actual != item['pages']: failures.append({'name':item['name'],'path':item['path'],'reason':'semantic mismatch','actual_counts':[len(p['events']) for p in actual], 'expected_counts':[len(p['events']) for p in item['pages']]})
            except Exception as error:
                failures.append({'name':item['name'],'error':repr(error)})
        result={'sessions':len(expected),'matched':len(expected)-len(failures),'failures':failures,'original_files_read_only':True}
        (HERE/'evidence/r6-transcripts/saved-pages-result.json').write_text(json.dumps(result,indent=2)+'\n')
        print(json.dumps(result))
        assert not failures

if __name__ == '__main__': main()
