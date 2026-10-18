"""Fresh isolated ACP request: one async task/turn snapshot, no original replay."""
import asyncio
import inspect
import json
import sys
from pathlib import Path

import pytest

OUT = Path('/home/ts/.cache/agent-scratch/nominal-pi-main/sqlite-acp-tasks.json')


def await_chain(coro):
    result = []
    while inspect.iscoroutine(coro):
        frame = coro.cr_frame
        result.append({'function': coro.cr_code.co_qualname,
                       'path': coro.cr_code.co_filename,
                       'line': frame.f_lineno if frame else None})
        coro = coro.cr_await
    result.append({'waiting_on': type(coro).__name__, 'repr': repr(coro)[:500]})
    return result


def sample():
    from agent_comms.backend import TurnSession
    tasks = [{'task': repr(t), 'awaits': await_chain(t.get_coro())}
             for t in asyncio.all_tasks()]
    turns = []
    for task, turn in TurnSession.active.items():
        turns.append({'task': repr(task), 'pid': turn.native.proc.pid,
                      'alive': turn.native.proc.alive(),
                      'fields': {k: type(v).__name__ for k, v in vars(turn).items()}})
    OUT.write_text(json.dumps({'tasks': tasks, 'turns': turns}, indent=2))


class Trace:
    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_call(self, item):
        # pytest-asyncio has selected its test runner, but has not started the loop.
        runner = item.funcargs.get('_function_scoped_runner')
        assert runner is not None, tuple(item.funcargs)
        timer = runner.get_loop().call_later(10, sample)
        try:
            yield
        finally:
            timer.cancel()


raise SystemExit(pytest.main(sys.argv[1:], plugins=[Trace()]))
