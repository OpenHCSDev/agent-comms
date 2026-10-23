"""Force two real CLI journal binders to contend at SQLite's write boundary."""
from __future__ import annotations

import multiprocessing
from multiprocessing.connection import Connection
from pathlib import Path

from agent_comms.cli_bridges.shared import Binding
from agent_comms.locked_store import SQLiteFile


def _bind(state: Path, requested: Binding, pipe: Connection) -> None:
    try:
        with Binding.open(state) as db:
            def trace(statement: str) -> None:
                if statement.startswith('BEGIN'):
                    pipe.send('begin')

            db.set_trace_callback(trace)
            pipe.send('ready')
            assert pipe.recv() == 'bind'
            try:
                pipe.send(('bound', requested.bind(db)))
            except ValueError as error:
                pipe.send(('rejected', str(error)))
    finally:
        pipe.close()


def _receive(pipe: Connection):
    assert pipe.poll(15), 'journal process did not reach the expected boundary'
    return pipe.recv()


def concurrent_bindings(state: Path, requests: tuple[Binding, Binding]) -> list[tuple]:
    context = multiprocessing.get_context('spawn')
    children = []
    pipes = []
    control = None
    try:
        for request in requests:
            parent, child = context.Pipe()
            pipes.append(parent)
            process = context.Process(target=_bind, args=(state, request, child))
            children.append(process)
            process.start()
            child.close()
        assert [_receive(pipe) for pipe in pipes] == ['ready', 'ready']
        control = SQLiteFile.writer(state / 'delivery.sqlite3')
        control.execute('BEGIN IMMEDIATE')
        for pipe in pipes:
            pipe.send('bind')
        # The old journal performs both SELECTs before its implicit BEGIN;
        # the repaired journal must begin before either SELECT. Hold SQLite's
        # real writer while both processes reach that transaction boundary.
        assert [_receive(pipe) for pipe in pipes] == ['begin', 'begin']
        control.commit()
        results = [_receive(pipe) for pipe in pipes]
        for process in children:
            process.join(15)
            assert process.exitcode == 0
        return results
    finally:
        if control is not None:
            control.close()
        for process in children:
            if process.is_alive():
                process.terminate()
            process.join(15)
        for pipe in pipes:
            pipe.close()
