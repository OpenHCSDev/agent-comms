"""The wire lock is synchronous custody: no wire scope may span an await.

At 00:51 UTC a worker deadlocked itself: its main thread blocked in a
synchronous exclusive acquisition (`claim_admission.py:51
verify_selected_wake` <- `channel_coding_tools.py:104,163` <-
`selected_tool_broker.py:443`) while an older acquisition on another
descriptor of the same `.wire.lock` was held by a suspended coroutine
(`queued_input.py:404 DeliveredInput.deliver_pending` awaiting
`Coordination.run_async` inside `async with _async_store_lock`). The suspended
holder has no frame in a thread dump, executors sit idle, and the loop thread
can never process the awaited result: permanent self-deadlock.
"""

import ast
import asyncio
import tempfile
import time
from pathlib import Path

from agent_comms.store_files import _async_store_lock, _store_lock

SCOPED = ("src/agent_comms/queued_input.py", "src/agent_comms/input_drain.py")


def _lock_scopes(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                call = item.context_expr
                if not isinstance(call, ast.Call):
                    continue
                func = call.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if name == "_async_store_lock":
                    yield node


def test_no_wire_lock_scope_spans_an_await():
    violations = []
    for filename in SCOPED:
        tree = ast.parse(Path(filename).read_text())
        for scope in _lock_scopes(tree):
            awaits = [node for node in ast.walk(scope) if isinstance(node, ast.Await)]
            if awaits:
                violations.append((filename, scope.lineno, len(awaits)))
    assert not violations, f"wire lock held across await: {violations}"


def test_waiter_admitted_while_holder_awaits_outside_lock(tmp_path):
    """The fixed pattern: sync work under custody, awaits after release."""
    wire = tmp_path / "wire"
    wire.write_text("{}")
    admitted = []

    async def holder():
        async with _async_store_lock(wire):
            pass
        await asyncio.sleep(1)

    async def main():
        task = asyncio.create_task(holder())
        await asyncio.sleep(0.2)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            try:
                with _store_lock(wire, blocking=False):
                    admitted.append(True)
                    break
            except BlockingIOError:
                await asyncio.sleep(0.05)
        await task

    asyncio.run(main())
    assert admitted == [True]
