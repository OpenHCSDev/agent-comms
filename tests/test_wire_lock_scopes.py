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
from contextlib import ExitStack
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest
from acp import RequestError

from agent_comms.agent_backend import InputContent
from agent_comms.input_attempt import ACPInputIdText, ReservedInput
from agent_comms.input_disposition import InputDispositions
from agent_comms.input_origin import UnattributedInputOrigin
from agent_comms.input_drain import InputDrain
from agent_comms.queued_input import DeliveredInput, QueuedInput
from agent_comms.store_files import _async_store_lock

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


def _harness(tmp_path):
    """Real dispositions and lock; plain-dict harnesses exactly as InputDrain holds them."""
    wire = tmp_path / "wire"
    wire.write_text("{}")
    inputs = SimpleNamespace(
        dispositions=InputDispositions(tmp_path / "input_dispositions.json"),
        following_sources={},
        queued_inputs={},
        backend_inboxes={},
        comms=SimpleNamespace(_wire_lock_path=wire),
    )
    inputs.require_inbox = MethodType(InputDrain.require_inbox, inputs)
    return inputs, wire


def _fill(inputs, session_id, count):
    for index in range(count):
        key = f"base-{index}"
        row = ReservedInput(key, None, "owner", 1, "owner", f"text {index}",
                            origin=UnattributedInputOrigin())
        custody = ExitStack()
        inputs.dispositions.reserve_originals(row, custody=custody)
        custody.pop_all()
        custody.close()
        inputs.following_sources.setdefault(session_id, {})[f"base-id-{index}"] = (
            SimpleNamespace(keys=(key,))
        )


def _delivered_item():
    return QueuedInput(
        text="delivered", echo=False,
        context=SimpleNamespace(active_goal_id=None),
        input_id=ACPInputIdText.new(), prompt="delivered",
    )


def _row_for(item):
    return ReservedInput(item.key, None, "owner", 1, "owner", item.text,
                         origin=UnattributedInputOrigin())


def test_last_slot_has_exactly_one_winner_on_production_paths(tmp_path):
    """Shepherd's interleaving against production code: one slot left, two
    contenders race the real locked decide-and-consume; exactly one places."""
    inputs, wire = _harness(tmp_path)
    session_id = "session"
    inbox = asyncio.Queue()
    inputs.backend_inboxes[session_id] = inbox
    _fill(inputs, session_id, 31)
    assert QueuedInput._remaining(inputs, session_id, inputs.dispositions.read()) == 1

    async def contender():
        item, content = _delivered_item(), InputContent(text="delivered")
        row, custody = _row_for(item), ExitStack()
        try:
            async with _async_store_lock(wire):
                DeliveredInput._decide_consume_unlocked(
                    inputs, session_id, inbox, item, content, row, custody)
        except RequestError:
            await asyncio.to_thread(custody.close)
            return None
        custody.pop_all()
        custody.close()
        return item

    async def main():
        return await asyncio.gather(contender(), contender())

    winners = [item for item in asyncio.run(main()) if item is not None]
    assert len(winners) == 1
    assert QueuedInput._remaining(inputs, session_id, inputs.dispositions.read()) == 0
    assert len(inputs.dispositions.read().rows) == 32
    assert inbox.qsize() == 1
