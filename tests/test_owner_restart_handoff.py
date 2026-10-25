"""Authored declaration controls: stopped witnesses are not live claim_scope.

No root, writer, child, native package or process launch is acquired here.
"""

from agent_comms.repo import Checkout
from dataclasses import replace
import os
from pathlib import Path
import pytest

from agent_comms.registry_document import ThreadRows

from agent_comms.child_process import ProcessIdentity
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import FieldCodec
from agent_comms.owner_launch import RetainedOwnerLaunch
from agent_comms.launch_runtime import LaunchRuntime, RestartEnvironment, SetVariable, UnsetVariable
from agent_comms.owner_lifecycle import OwnerRestartSelection
from agent_comms.owner_restart import OwnerRestartHandoff, RetiredOwnerLaunch
from agent_comms.registry_document import RegistryDocument, RegistrySnapshot
from agent_comms.thread_identity import GenerationCounter
from agent_comms.thread_status import StoppedThreadStatus
from agent_comms.native_arguments import ConfiguredBinary, InheritedArguments
from agent_comms.threads import Thread


@pytest.mark.parametrize("source", [{}, {"PYTHONPATH": ""}, {"PYTHONPATH": "/source"}])
def test_import_path_preserves_absent_and_empty_then_removes_explicitly(source):
    configuration = RestartEnvironment.inherit(source)
    encoded = configuration.encode()
    assert {key: value for key, value in encoded.items() if key == "PYTHONPATH"} == source
    assert FieldCodec.decode(RestartEnvironment, encoded) == configuration
    assert "PYTHONPATH" not in configuration.without_pythonpath().apply_runtime(source)


@pytest.mark.parametrize(('inherited', 'expected'), [
    (UnsetVariable(), '/runtime/bin' + os.pathsep + os.defpath),
    (SetVariable(''), '/runtime/bin:'),
    (SetVariable('/usr/bin'), '/runtime/bin:/usr/bin'),
    (SetVariable('/runtime/bin:/usr/bin:/runtime/bin'), '/runtime/bin:/usr/bin'),
])
def test_runtime_path_is_idempotent_and_keeps_other_search_entries(inherited, expected):
    interpreter = Path('/runtime/bin/python')
    original = RestartEnvironment(path=inherited)
    configured = original.for_interpreter(interpreter)
    path = configured.path.otherwise('')
    entries = path.split(os.pathsep)
    assert entries[0] == '/runtime/bin'
    assert entries.count('/runtime/bin') == 1
    assert path == expected
    assert configured.for_interpreter(interpreter) == configured
    applied = configured.apply_runtime({'PATH': inherited.otherwise(''), 'AUTHORED_CREDENTIAL': 'kept'})
    assert applied['PATH'] == expected
    assert applied['AUTHORED_CREDENTIAL'] == 'kept'


def stopped(registry):
    # A declared absent synthetic process, never a borrowed original identity.
    thread = Thread("authored", frozenset(), Checkout.from_wire("/authored"),
                    process_identity=ProcessIdentity(2147483647, 1), created_at=1.0)
    snapshot = RegistrySnapshot.capture(RegistryDocument(
        threads=ThreadRows.from_threads({thread.name: thread}), statuses={thread.name: StoppedThreadStatus()},
        last_seen={thread.name: 1.0},
        owners=GenerationCounter(4, {thread.name: 4}),
        claims=GenerationCounter(5, {thread.name: 5}),
    ))
    return thread, snapshot


def test_retired_witness_cannot_borrow_live_or_replaced_claim(*, thread_registration):
    thread, snapshot = stopped(registry=thread_registration)
    launch = RetainedOwnerLaunch(thread.require_process(), "/authored/python",
                                 {"AGENT_COMMS_AGENT_BIN": "/authored/pi"})
    owner = RetiredOwnerLaunch.capture_retired(snapshot, thread, launch)
    assert owner.require_current(snapshot) is thread
    selection = OwnerRestartSelection(
        owner.authority.owner, launch.process, owner.authority.claim.claimed_generation,
        launch.interpreter,
    )
    with pytest.raises(RelationViolationError):
        selection.require_current(snapshot)
    changed = (
        replace(snapshot, owner_generations={thread.name: 6}),
        replace(snapshot, _claimed_generations={thread.name: 6}),
        replace(snapshot, threads=ThreadRows.from_threads({thread.name: replace(thread, created_at=2.0)})),
        replace(snapshot, threads=ThreadRows.from_threads({thread.name: replace(
            thread, process_identity=ProcessIdentity(2147483646, 1))})),
    )
    for substituted in changed:
        with pytest.raises(RelationViolationError):
            owner.require_current(substituted)


def test_handoff_codec_preserves_complete_stopped_witness_and_original_launch(*, thread_registration):
    thread, snapshot = stopped(registry=thread_registration)
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
    original = OwnerRestartHandoff("/authored", (owner,), runtime, ConfiguredBinary(), InheritedArguments(), LaunchRuntime(Path("/target/bin/python")))
    encoded = FieldCodec.encode(original)
    decoded = FieldCodec.decode(OwnerRestartHandoff, encoded)
    assert decoded == original
    assert decoded.owners[0].require_current(snapshot) is thread
    assert decoded.owners[0].launch.arguments == ("--no-tools", "authored value")
    assert decoded.owners[0].launch.environment["AUTHORED_CREDENTIAL"] == "in-memory-only"
    stored = replace(thread, storage_roots=thread_registration.storage_rule.initial_roots(thread.name))
    assert "execution" not in FieldCodec.encode(stored)
    unrelated = replace(launch, process=ProcessIdentity(2147483646, 1))
    with pytest.raises(RelationViolationError):
        replace(owner, launch=unrelated).require_current(snapshot)


@pytest.mark.parametrize(("environment", "arguments"), [
    ({}, ()),
    ({"AGENT_COMMS_AGENT_ARGS": ""}, ()),
    ({"AGENT_COMMS_AGENT_ARGS": "--no-tools 'authored value'"}, ("--no-tools", "authored value")),
])
def test_retained_arguments_restart_from_the_original_environment(environment, arguments):
    launch = RetainedOwnerLaunch(ProcessIdentity(2147483647, 1), "/authored/python", environment)
    assert launch.arguments == arguments
    assert InheritedArguments().restarted(launch.arguments).argv == arguments


def test_retirement_capture_refuses_changed_original_incarnation(*, thread_registration):
    thread, snapshot = stopped(registry=thread_registration)
    launch = RetainedOwnerLaunch(thread.require_process(), "/authored/python",
                                 {"AGENT_COMMS_AGENT_BIN": "/authored/pi"})
    with pytest.raises(RelationViolationError):
        RetiredOwnerLaunch.capture_retired(snapshot, replace(thread, created_at=2.0), launch)


