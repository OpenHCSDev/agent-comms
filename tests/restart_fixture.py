"""Original restart records from real registry declarations and a retired process."""

import json
import subprocess
import sys
from dataclasses import replace

import pytest

import agent_comms.restart_queue as queue
from agent_comms.child_process import ProcessIdentity
from agent_comms.field_codec import FieldCodec, RestartId
from agent_comms.launch_runtime import RestartEnvironment
from agent_comms.owner_lifecycle import OwnerRestartSelection
from agent_comms.repo import Checkout
from agent_comms.threads import Thread

@pytest.fixture
def dead_owner_restart(private_comms):
    comms = private_comms
    comms.registry.declare(Thread('worker', frozenset(), Checkout(comms.root)))
    snapshot = comms.registry.snapshot()
    with subprocess.Popen(
        (sys.executable, '-c', 'import sys; sys.stdin.buffer.read(1)'), stdin=subprocess.PIPE,
    ) as process:
        identity = ProcessIdentity.capture(process.pid)
        process.communicate(b'x', timeout=10)
        assert process.returncode == 0
    selection = OwnerRestartSelection(
        snapshot.owner_identity('worker'), identity,
        snapshot.claim_generation('worker'), sys.executable,
    )
    return queue.QueuedRestart(
        RestartId.mint(), selection,
        queue.RestartTarget(sys.executable, RestartEnvironment(), sys.executable),
        queue.PendingRestart(),
    )


@pytest.fixture
def original_restart_json(private_comms, dead_owner_restart):
    comms = private_comms
    directory = comms.root / queue.DIRECTORY
    directory.mkdir(mode=0o700)
    pending = dead_owner_restart
    comms.registry.declare(Thread('attempted', frozenset(), Checkout(comms.root)))
    attempted = replace(pending, id=RestartId.mint(), state=queue.AttemptingRestart(),
                        selection=replace(pending.selection,
                            identity=comms.registry.snapshot().owner_identity('attempted')))
    terminal = replace(pending, id=RestartId.mint(),
                       state=queue.UncertainRestart(reason='retain ambiguous terminal'))
    comms.registry.declare(Thread('interrupted', frozenset(), Checkout(comms.root)))
    interrupted = replace(terminal, id=RestartId.mint(),
                          selection=replace(pending.selection,
                              identity=comms.registry.snapshot().owner_identity('interrupted')))
    paths = []
    for record in (pending, attempted, terminal, interrupted):
        parent = directory / ('settled/' + str(record.owner.lookup)
                              if record is terminal else 'active')
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = parent / f'{record.id if record is terminal else record.owner.lookup}.json'
        path.write_text(json.dumps(FieldCodec.encode_fields(record)))
        paths.append(path)
    return {path: record for path, record in zip(paths, (pending, attempted, terminal, interrupted), strict=True)}
