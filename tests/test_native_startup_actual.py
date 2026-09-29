"""Saved native preflight with actual pipes; no provider or protocol stand-ins."""

import asyncio
import select
import signal
import time
from contextlib import aclosing

import pytest

from agent_comms import backend
from agent_comms.native_pi import NativePiUnavailable
from agent_comms.native_session_prepare import NativeSessionPreparation
from agent_comms.native_startup import NativeStartupAdmission
from agent_comms.session_fence import session_writer_fence

pytest_plugins = ("test_backend_native_lifecycle",)


class BusyOwnerPreparation(NativeSessionPreparation):
    """Block only the Python owner; the real child restores the saved session."""

    def wait_for_native_pipe(self):
        pipe = self.native.proc.process._transport.get_pipe_transport(1).get_extra_info("pipe")
        readable, _, _ = select.select([pipe], [], [], 8)
        self.pipe_ready = bool(readable)
        # Ensure the configured short test deadline passed, without replacing
        # native initialization, its response, or the tracked reader.
        time.sleep(max(0, self.watchdog.preflight_deadline - time.monotonic()) + 0.02)


class BeforeReadPreparation(BusyOwnerPreparation):
    async def receive_record(self):
        self.wait_for_native_pipe()
        async for event in super().receive_record():
            yield event


class DuringReadPreparation(BusyOwnerPreparation):
    async def initialize_rpc(self):
        async for event in super().initialize_rpc():
            yield event
        self.loop.call_later(0.01, self.wait_for_native_pipe)


class StoppedNativePreparation(NativeSessionPreparation):
    async def receive_record(self):
        self.native.proc.platform.send(self.native.proc.identity, signal.SIGSTOP)
        async for event in super().receive_record():
            yield event


async def prepare(owner, preparation_type, monkeypatch):
    seeded = await owner.run("Saved history, never replay this input")
    assert seeded[-1].ok, seeded[-1]
    launch = owner.persistent.custody.child.key[0]
    await owner.persistent.discard_for_external_write(str(owner.session))
    monkeypatch.setattr(backend, "CAPABILITY_PREFLIGHT_TIMEOUT_SECONDS", 0.05)
    admission = NativeStartupAdmission(owner.root)
    preparation = preparation_type(
        launch,
        "",
        session_file=str(owner.session),
        persistent_session=owner.persistent,
        startup=admission,
    )
    try:
        async with (
            asyncio.timeout(12),
            session_writer_fence(str(owner.session)),
            owner.persistent.lock,
            aclosing(preparation.run()) as stream,
        ):
            async for _ in stream:
                pass
    finally:
        admission.release()
        assert owner.provider.posts == len(owner.starts) == len(owner.saved_inputs()) == 1
        assert (
            owner.saved_inputs()[0]["content"][0]["text"]
            == "Saved history, never replay this input"
        )
    return preparation


@pytest.mark.parametrize("preparation_type", [BeforeReadPreparation, DuringReadPreparation])
async def test_actual_saved_native_ready_pipe_survives_busy_owner(
    native_backend, monkeypatch, preparation_type
):
    preparation = await prepare(native_backend, preparation_type, monkeypatch)
    assert preparation.pipe_ready
    assert preparation.native.attestation.observed
    assert preparation.native.attestation.identity.session_file == str(native_backend.session)
    assert preparation.native.attestation.state.pending_message_count == 0
    assert native_backend.persistent.custody.idle().current


@pytest.mark.skipif(not hasattr(signal, "SIGSTOP"), reason="Native stop observation needs POSIX")
async def test_actual_unresponsive_native_keeps_deadline(native_backend, monkeypatch):
    with pytest.raises(NativePiUnavailable, match="preflight timed out"):
        await prepare(native_backend, StoppedNativePreparation, monkeypatch)
    assert not native_backend.persistent.available
