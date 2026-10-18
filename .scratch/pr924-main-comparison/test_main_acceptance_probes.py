"""External #924 acceptance probes; all production imports are pinned main.

Main has no launch-expiry test or ChildRetirementCancelled class/parameter.
The first probe preserves #924's assertions and only releases its injected exec
hold in finally, so main's unbounded launch cannot strand fixture cleanup.
The second checks the completed-cancellation behavior using stdlib cancellation;
it is explicitly NOT the absent #924 exception-capability parameter.
"""
import asyncio
import sys

import pytest

from test_recovery_gateway import private_root, request

pytest_plugins = ("test_backend_native_lifecycle",)


async def test_clean_launch_expiry_keeps_same_gateway_available(private_root, monkeypatch):
    from agent_comms.child_process import Platform
    from agent_comms.recovery_gateway import RecoveryGateway
    from agent_comms.recovery_gateway_client import read_gateway_projection

    with Platform.current().launch((sys.executable, "-c", "pass"), ()) as launch:
        launch_type = type(launch)
    original_verify, original_release = launch_type.verify_async, launch_type.release
    captured, verified = [], []
    gate = asyncio.Event()
    def release(launch, identity):
        captured.append(identity)
        original_release(launch, identity)
    async def verify(launch):
        await original_verify(launch)
        verified.append(True)
        if len(verified) == 1:
            await gate.wait()
    monkeypatch.setattr(launch_type, "release", release)
    monkeypatch.setattr(launch_type, "verify_async", verify)
    gateway = RecoveryGateway(private_root)
    await gateway.start()
    try:
        import json
        assert json.loads(await request(gateway.path))["availability"] == "unavailable"
        assert verified == [True]
        assert len(captured) == 1
        assert not captured[0].alive()
        assert not gateway._orphaned
        assert not gateway._closing
        view = await read_gateway_projection(gateway.path, "Alice")
        assert view["availability"] == "available"
        assert view["owner"] == "Alice"
        assert view["current"]["status"] == "pending"
    finally:
        gate.set()  # Only retire the deliberately held baseline launch after assertions.
        await gateway.close()
    assert verified == [True, True]
    assert not gateway.path.exists()
    assert gateway._lock_fd is None
    assert all(not identity.alive() for identity in captured)


async def test_completed_native_cancellation_does_not_poison_new_input(native_backend, monkeypatch):
    from agent_comms.native_custody import PiSessionChild

    owner = native_backend
    assert (await owner.run("Fresh main-only completed-cancellation probe"))[-1].ok
    previous = owner.persistent.custody.child.proc
    close = PiSessionChild.close
    async def failed_close(child):
        await close(child)
        if child.proc is previous:
            assert not child.proc.alive()
            raise asyncio.CancelledError("completed main cleanup cancellation")
    monkeypatch.setattr(PiSessionChild, "close", failed_close)
    retiring = owner.persistent.custody.retire()
    await asyncio.wait((retiring.task,))
    print("completed_native_job", previous.identity, "physically_retired", previous.retired, flush=True)
    with pytest.raises(asyncio.CancelledError):
        await owner.persistent._finish_retirement(retiring)
    assert not previous.alive()
    await owner.persistent.close_idle()
    result = await owner.run("Distinct new main-only input after successful child close")
    assert result[-1].ok, result[-1]
    assert owner.provider.posts == len(owner.saved_inputs()) == 2
    assert owner.persistent.custody.child.proc is not previous
