"""One fresh ACP tool-error diagnostic; never opens either frozen attempt."""
import itertools
import json
import sqlite3
import sys
import threading
import time
import traceback
from pathlib import Path

import pytest

out = Path('/home/ts/.cache/agent-scratch/nominal-pi-main/sqlite-holder-trace.jsonl').open('w')
lock = threading.Lock()
active = {}
numbers = itertools.count(1)
original_connect = sqlite3.connect


def emit(event, **fields):
    with lock:
        out.write(json.dumps({'at': time.monotonic(), 'thread': threading.current_thread().name,
                              'event': event, **fields}) + '\n')
        out.flush()


class TracedConnection(sqlite3.Connection):
    def execute(self, sql, *args, **kwargs):
        tracked = getattr(self, '_trace_id', None)
        important = sql.lstrip().upper().startswith(('BEGIN', 'COMMIT', 'ROLLBACK', 'PRAGMA SYNCHRONOUS'))
        if tracked and important:
            emit('enter', connection=tracked, sql=sql, stack=traceback.format_stack(limit=14))
        begun = time.monotonic()
        try:
            result = super().execute(sql, *args, **kwargs)
        except BaseException as error:
            if tracked:
                emit('error', connection=tracked, sql=sql[:250], error=repr(error), elapsed=time.monotonic()-begun)
            raise
        else:
            if tracked:
                with lock:
                    active[tracked]['transaction'] = self.in_transaction
                    active[tracked]['last_sql'] = sql[:250]
                if important or time.monotonic()-begun > .05:
                    emit('exit', connection=tracked, sql=sql[:250], transaction=self.in_transaction,
                         elapsed=time.monotonic()-begun)
            return result

    def close(self):
        tracked = getattr(self, '_trace_id', None)
        if tracked:
            emit('close', connection=tracked, transaction=self.in_transaction)
            with lock:
                active.pop(tracked, None)
        return super().close()


def connect(path, *args, **kwargs):
    if 'coordination.sqlite3' not in str(path):
        return original_connect(path, *args, **kwargs)
    assert 'factory' not in kwargs, 'Do not replace another connection capability'
    db = original_connect(path, *args, factory=TracedConnection, **kwargs)
    db._trace_id = next(numbers)
    with lock:
        active[db._trace_id] = {'path': str(path), 'thread': threading.current_thread().name,
                                'transaction': False, 'last_sql': None}
    emit('open', connection=db._trace_id, path=str(path), stack=traceback.format_stack(limit=12))
    return db


def sample():
    with lock:
        snapshot = {k: dict(v) for k, v in active.items()}
    stacks = {str(i): traceback.format_stack(f) for i, f in sys._current_frames().items()}
    emit('holder-sample', active=snapshot, stacks=stacks)


class Trace:
    def pytest_sessionstart(self, session):
        sqlite3.connect = connect
        self.timer = threading.Timer(10, sample)
        self.timer.daemon = True
        self.timer.start()

    def pytest_sessionfinish(self, session, exitstatus):
        self.timer.cancel()
        sample()
        sqlite3.connect = original_connect


raise SystemExit(pytest.main(sys.argv[1:], plugins=[Trace()]))
