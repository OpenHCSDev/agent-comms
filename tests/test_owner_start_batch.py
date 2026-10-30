"""Real shared-host owner starts, with no new input or provider execution."""
from __future__ import annotations

from pathlib import Path

import pytest

from agent_comms.cli_verbs.restore_owner import RestoreOwnerCliCommand
from agent_comms.cli_verbs.start import StartCliCommand
from agent_comms.comms import Comms, wire
from agent_comms.errors import UnregisteredThreadError
from agent_comms.field_codec import FieldCodec
from agent_comms.native_package import VerifiedNativePackage
from agent_comms.owner_connection import OwnerConnection
from agent_comms.owner_launch import ReadOwnerLaunchRuntimeRequest, RetainedOwnerLaunch
from agent_comms.repo import Checkout
from agent_comms.threads import Thread
from bus_record_fixture import prepare_native_comms


def attached_launch(comms: Comms, name: str) -> RetainedOwnerLaunch:
    snapshot = comms.registry.snapshot()
    process = snapshot.require(name).require_process()
    return FieldCodec.decode(RetainedOwnerLaunch, OwnerConnection.exchange(
        comms.root, process,
        ReadOwnerLaunchRuntimeRequest(thread=name, owner=snapshot.owner_identity(name)),
    ))


@pytest.mark.parametrize("command", (RestoreOwnerCliCommand, StartCliCommand))
def test_start_batch_attaches_every_owner_to_one_host(
    tmp_path: Path, native_runtime_package: VerifiedNativePackage,
    command: type[RestoreOwnerCliCommand],
) -> None:
    comms = prepare_native_comms(wire(tmp_path / "wire"))
    names = ("first", "second")
    for name in names:
        comms.registry.declare(Thread(name, frozenset(), Checkout(tmp_path)))
    try:
        results = command.apply_together(comms, tuple(command(name=name) for name in names))
        assert tuple(result.thread for result in results) == names
        assert all(result.launched for result in results)
        first, second = (attached_launch(comms, name) for name in names)
        assert first.process == second.process and first.process.alive()
        assert {result.pid for result in results} == {first.process.pid}
        assert all(not command(name=name).apply(comms).launched for name in names)
    finally:
        comms.owners.stop(*names)
    assert all(not comms.registry.require(name).process_alive for name in names)


def test_later_selection_failure_joins_the_already_dispatched_start(
    tmp_path: Path, native_runtime_package: VerifiedNativePackage,
) -> None:
    comms = prepare_native_comms(wire(tmp_path / "wire"))
    comms.registry.declare(Thread("first", frozenset(), Checkout(tmp_path)))
    try:
        with pytest.raises(UnregisteredThreadError):
            comms.owners.start_many(("first", "missing"))
        # The first request was sent once. Failure of another selection must
        # join its attachment, not close the reply pipe or roll it back.
        launch = attached_launch(comms, "first")
        assert launch.process == comms.registry.require("first").require_process()
        assert not comms.owners.start_many(("first",))[0].launched
    finally:
        comms.owners.stop("first")
