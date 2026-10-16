"""Canonical ACP owners sharing the production bus, SQL stores and owner socket."""

import os
from contextlib import asynccontextmanager
from pathlib import Path

from agent_comms.acp import CommsAgent


def native_model(*, decision="FULL", fail_on=None):
    """Use the shared model boundary with the production selected-session owner."""
    from test_coordinated_runtime import _fake_model

    return _fake_model(decision=decision, fail_on=fail_on)


@asynccontextmanager
async def canonical_delivery_owner(root, *, direct=False):
    """Only model execution is supplied by tests; routing and persistence are real."""
    from test_coordinated_runtime import _root

    _path, root_id, comms, initial, people = _root(root, direct=direct)
    owner = CommsAgent(
        comms,
        runtime_enabled=True,
        private_nk_native_package=root,
        private_nk_wire_root_id=root_id,
    )
    for person in people[1:]:
        owner.sessions.bindings[person.name] = person.name
        owner.sessions.titles[person.name] = person.name
        owner.sessions.worktrees[person.name] = person.worktree
    await owner._runtime.start()
    try:
        yield comms, owner, initial.message, root_id
    finally:
        await owner.shutdown()


def canonical_agent(comms, **options):
    """Configure the existing owner against its real canonical root marker."""
    with comms.bus.log.locked():
        metadata = comms.bus.log.read_metadata_unlocked()
    root_id = (
        metadata.root_id if metadata.private
        else comms.messaging.initialize_private_initial_protocol()
    )
    package = Path(os.environ["PI_COMPACTION_TEST_PACKAGE"])
    return CommsAgent(
        comms, private_nk_native_package=package,
        private_nk_wire_root_id=root_id, **options,
    )


async def reply_turn_finished(owner, session):
    """Wait for the reply turn a drain scheduled; the task removes itself when it ends."""
    task = owner.inputs.wake_tasks.get(session)
    if task is not None:
        await task


def drain_diagnostic(comms, thread):
    """What the owner records on its thread when its inbox drain failed."""
    from agent_comms.comms import Comms

    view = next(
        view for view in Comms(comms.root).views.thread_views() if view.thread.name == thread
    )
    return view.activity.readiness.source_diagnostic()
