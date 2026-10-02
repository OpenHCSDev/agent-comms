"""Observe actual installed original reads; instrument, never replace the stores."""
import hashlib
import json
import pathlib
import runpy
import sqlite3
import sys
from contextlib import contextmanager
from unittest.mock import patch

from agent_comms import coordinated_runtime_schema, coordination_response
from agent_comms.native_entries import NativeEntry
from agent_comms.native_runtime_input import NativeRuntimeInput
from agent_comms.transcript_receipts import AssignedTranscriptSource
from agent_comms.transcripts import Transcripts

repo = pathlib.Path('/home/ts/wt/comms-context-manifest-resource-20261002')
output = repo / '.artifacts/native-stage-display'
pages, reads, page_stack, read_stack = [], [], [], []
wire_calls = render_calls = 0
read_original = NativeRuntimeInput._publication_read
page_original = Transcripts.thread_transcript_page
wire_original = AssignedTranscriptSource.rows
render_original = NativeEntry.events


@contextmanager
def observe_read(cls, root):
    frame = sys._getframe()
    while frame is not None and frame.f_code.co_name not in ('native_records', 'publication_revision'):
        frame = frame.f_back
    kind = frame.f_code.co_name if frame is not None else 'direct_proof_fragment'
    fragment = frame.f_locals.get('fragment', ()) if frame is not None else ()
    observation = dict(kind=kind, page=len(pages)-1 if page_stack else None,
                       records=len(fragment), decoded_entries=sum(r.entry is not None for r in fragment),
                       connections=0, native_schema_checks=0, response_schema_checks=0,
                       source_queries=0, reply_queries=0, closed=False)
    reads.append(observation)
    read_stack.append(observation)
    db = None
    try:
        with read_original(root) as db:
            if db is not None:
                observation['connections'] += 1

                def statement(sql):
                    if sql.startswith('SELECT'):
                        key = 'source_queries' if NativeRuntimeInput.declared_name in sql else 'reply_queries'
                        observation[key] += 1

                db.set_trace_callback(statement)
            yield db
    finally:
        read_stack.pop()
        if db is not None:
            try:
                db.execute('SELECT 1')
            except sqlite3.ProgrammingError:
                observation['closed'] = True
            else:
                raise AssertionError('original projection connection remained open')


def observe_schema(original, key):
    def checked(db):
        read_stack[-1][key] += 1
        return original(db)
    return checked


def wire_rows(self, *args, **kwargs):
    global wire_calls
    assert not read_stack, 'coordinator scope overlaps original wire read'
    wire_calls += 1
    return wire_original(self, *args, **kwargs)


def render(self, projection):
    global render_calls
    assert not read_stack, 'coordinator scope overlaps native display rendering'
    render_calls += 1
    return render_original(self, projection)


def page(self, name, **kwargs):
    observation = dict(owner=name, direction='later' if kwargs.get('after') is not None else 'earlier',
                       max_messages=kwargs.get('max_messages', 20), max_bytes=kwargs.get('max_bytes', 65536))
    pages.append(observation)
    page_stack.append(observation)
    try:
        result = page_original(self, name, **kwargs)
        observation.update(events=len(result.events), before=result.before.offset, after=result.after.offset)
        return result
    finally:
        page_stack.pop()


with patch.object(NativeRuntimeInput, '_publication_read', classmethod(observe_read)), \
     patch.object(coordinated_runtime_schema, 'assert_native_runtime_schema', observe_schema(coordinated_runtime_schema.assert_native_runtime_schema, 'native_schema_checks')), \
     patch.object(coordination_response, '_assert_response_schema', observe_schema(coordination_response._assert_response_schema, 'response_schema_checks')), \
     patch.object(AssignedTranscriptSource, 'rows', wire_rows), \
     patch.object(NativeEntry, 'events', render), \
     patch.object(Transcripts, 'thread_transcript_page', page):
    runpy.run_path(str(repo / 'evidence/native-stage-transcript-display-20261002/installed_saved_read.py'), run_name='__main__')

assert reads and all(r['closed'] for r in reads)
assert all(r['connections'] == r['native_schema_checks'] == r['response_schema_checks'] == 1 for r in reads)
assert wire_calls and render_calls
for index, observation in enumerate(pages):
    fragments = [r for r in reads if r['page'] == index and r['kind'] == 'native_records']
    observation.update(fragments=len(fragments), decoded_entries=sum(r['decoded_entries'] for r in fragments),
                       connections=sum(r['connections'] for r in fragments),
                       source_queries=sum(r['source_queries'] for r in fragments),
                       reply_queries=sum(r['reply_queries'] for r in fragments))
receipt = dict(state='PASS', installed_source=json.loads((output/'installed-saved-receipt.json').read_text())['source_head'],
               pages=pages, reads=reads, wire_calls=wire_calls, native_render_calls=render_calls,
               all_connections_closed=True, coordinator_overlaps_wire_or_render=0,
               query_scope='source/reply SELECTs after schema verification; each native/response schema assertion counted separately',
               wheel_sha256=hashlib.file_digest((output/'wheels/agent_comms-0.1.0-py3-none-any.whl').open('rb'), 'sha256').hexdigest(),
               provider_calls=0, native_inputs=0, public_mutations=[],
               scope='Existing installed saved-reader journey, original public SQL/native/wire, forwarding observation wrappers only.')
(output/'installed-fragment-read.json').write_text(json.dumps(receipt, indent=2)+'\n')
print(json.dumps(dict(state='PASS', pages=pages, wire_calls=wire_calls, native_render_calls=render_calls,
                      coordinator_overlaps_wire_or_render=0)))
