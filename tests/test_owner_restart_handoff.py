"""Authored declaration controls: stopped witnesses are not live admission.

No root, writer, child, native package or process launch is acquired here.
"""

from dataclasses import replace
import pytest

from agent_comms.child_process import ProcessIdentity
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import FieldCodec
from agent_comms.owner_launch import RestartEnvironment, RetainedOwnerLaunch
from agent_comms.owner_lifecycle import OwnerRestartSelection
from agent_comms.owner_restart import OwnerRestartHandoff, RetiredOwnerLaunch
from agent_comms.registry_document import RegistrySnapshot
from agent_comms.thread_status import RunningThreadStatus, StoppedThreadStatus
from agent_comms.threads import Thread


def stopped():
    # A declared absent synthetic process, never a borrowed original identity.
    thread = Thread("authored", frozenset(), "/authored",
                    process_identity=ProcessIdentity(2147483647, 1), created_at=1.0)
    snapshot = RegistrySnapshot(
        threads={thread.name: thread}, statuses={thread.name: StoppedThreadStatus()},
        last_seen={thread.name: 1.0}, aliases={},
        owner_generations={thread.name: 4}, admission_generations={thread.name: 5},
    )
    return thread, snapshot


def test_retired_witness_cannot_borrow_live_or_replaced_admission():
    thread, snapshot = stopped()
    launch = RetainedOwnerLaunch(thread.require_process(), "/authored/python",
                                 {"AGENT_COMMS_AGENT_BIN": "/authored/pi"})
    owner = RetiredOwnerLaunch.capture_retired(snapshot, thread, launch)
    assert owner.require_current(snapshot) is thread
    selection = OwnerRestartSelection(owner.owner, launch.process, owner.admission.admission_generation)
    with pytest.raises(RelationViolationError):
        selection.require_current(snapshot)
    changed = (
        replace(snapshot, owner_generations={thread.name: 6}),
        replace(snapshot, admission_generations={thread.name: 6}),
        replace(snapshot, statuses={thread.name: RunningThreadStatus()}),
        replace(snapshot, threads={thread.name: replace(thread, created_at=2.0)}),
        replace(snapshot, threads={thread.name: replace(
            thread, process_identity=ProcessIdentity(2147483646, 1))}),
    )
    for substituted in changed:
        with pytest.raises(RelationViolationError):
            owner.require_current(substituted)


def test_handoff_codec_preserves_complete_stopped_witness_and_original_launch():
    thread, snapshot = stopped()
    environment = {
        "AGENT_COMMS_THREAD": thread.name, "AGENT_COMMS_AGENT_BIN": "/authored/pi",
        "AGENT_COMMS_AGENT_ARGS": "--no-tools 'authored value'",
        "PI_CODING_AGENT_DIR": "/authored/config",
        "AGENT_COMMS_NATIVE_CONFIG_DIR": "/authored/config",
        "AUTHORED_CREDENTIAL": "in-memory-only",
    }
    launch = RetainedOwnerLaunch(thread.require_process(), "/authored/python", environment)
    owner = RetiredOwnerLaunch.capture_retired(snapshot, thread, launch)
    runtime = RestartEnvironment.inherit(environment)
    original = OwnerRestartHandoff("/authored", (owner,), runtime, None, None)
    encoded = FieldCodec.encode(original)
    assert set(encoded["owners"][0]) == {"owner", "admission", "launch"}
    decoded = FieldCodec.decode(OwnerRestartHandoff, encoded)
    assert decoded == original
    assert decoded.owners[0].require_current(snapshot) is thread
    assert decoded.owners[0].launch.arguments == ("--no-tools", "authored value")
    assert decoded.owners[0].launch.environment["AUTHORED_CREDENTIAL"] == "in-memory-only"
    assert "execution" not in FieldCodec.encode(thread)
    unrelated = replace(launch, process=ProcessIdentity(2147483646, 1))
    with pytest.raises(RelationViolationError):
        replace(owner, launch=unrelated).require_current(snapshot)


def test_retirement_capture_refuses_changed_original_incarnation():
    thread, snapshot = stopped()
    launch = RetainedOwnerLaunch(thread.require_process(), "/authored/python",
                                 {"AGENT_COMMS_AGENT_BIN": "/authored/pi"})
    with pytest.raises(RelationViolationError):
        RetiredOwnerLaunch.capture_retired(snapshot, replace(thread, created_at=2.0), launch)


def test_handoff_restore_requires_each_authentic_interpreter_before_read_or_launch():
    from pathlib import Path
    from types import SimpleNamespace

    thread, snapshot = stopped()
    launch = RetainedOwnerLaunch(thread.require_process(), "/authored/other-python",
                                 {"AGENT_COMMS_AGENT_BIN": "/authored/pi"})
    owner = RetiredOwnerLaunch.capture_retired(snapshot, thread, launch)
    handoff = OwnerRestartHandoff("/authored", (owner,), RestartEnvironment.inherit({}), None, None)
    lifecycle = SimpleNamespace(root=Path("/authored"))
    # No registry/launch member supplied: refusal must precede either access.
    with pytest.raises(RelationViolationError, match="acquired source interpreter"):
        handoff.restore(lifecycle)
