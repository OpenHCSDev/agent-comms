"""Saved native preflight with actual pipes; no provider or protocol stand-ins."""

import asyncio
import json
import os
import select
import signal
import time
from contextlib import ExitStack, aclosing
from pathlib import Path

import pytest

from agent_comms import backend
from agent_comms.diagnostics import record_terminal_failure
from agent_comms.native_pi import NativePiInputNotSent, NativePiUnavailable
from agent_comms.native_session_prepare import NativeSessionPreparation
from agent_comms.native_startup import NativeStartupAdmission
from agent_comms.pi_rpc import PiRpcChannel
from agent_comms.session_fence import session_writer_fence
from agent_comms.tracked_turn import TrackedTurnSession

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


async def test_actual_tracked_ready_pipe_survives_busy_owner(native_backend, monkeypatch):
    """Same native response/read race as preparation, through tracked input admission."""
    owner = native_backend
    original_read = PiRpcChannel.readline
    ready = []

    async def busy_read(channel, **options):
        if not ready:
            pipe = channel.reader._transport.get_extra_info("pipe")
            readable, _, _ = select.select([pipe], [], [], 8)
            ready.append(bool(readable))
            # Stall only this Python owner's scheduling, while native's actual
            # correlated get_state response is available in the original pipe.
            time.sleep(0.08)
        return await original_read(channel, **options)

    monkeypatch.setattr(backend, "CAPABILITY_PREFLIGHT_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(PiRpcChannel, "readline", busy_read)
    result = await TrackedTurnSession.execute(
        Path(os.environ["PI_COMPACTION_TEST_PACKAGE"]),
        input_id="48acac7a89e0442b95c21cbe26cdc965",
        prompt="Controlled tracked initialization input, sent exactly once",
        worktree=owner.project,
        session_dir=owner.session.parent,
        session_file=owner.session,
        provider="response-local",
        model="fixture",
        thinking_level="off",
        maintenance_root=owner.root,
    )
    assert ready == [True]
    assert result.text == owner.provider.text
    assert owner.provider.posts == len(owner.saved_inputs()) == 1
    assert owner.saved_inputs()[0]["inputId"] == result.context.input_id


@pytest.mark.skipif(not hasattr(signal, "SIGSTOP"), reason="Native stop observation needs POSIX")
async def test_actual_tracked_unresponsive_native_owns_not_sent_witness(
    native_backend, monkeypatch
):
    owner = native_backend
    children = []

    class StoppedTracked(TrackedTurnSession):
        async def next_event(self):
            children.append(self.native.proc)
            self.native.proc.platform.send(self.native.proc.identity, signal.SIGSTOP)
            return await super().next_event()

    monkeypatch.setattr(backend, "CAPABILITY_PREFLIGHT_TIMEOUT_SECONDS", 0.05)
    with pytest.raises(NativePiInputNotSent) as failed:
        await StoppedTracked.execute(
            Path(os.environ["PI_COMPACTION_TEST_PACKAGE"]),
            input_id="ba2b5029200e49eea84bf0db17d2316c",
            prompt="Private unadmitted input, never replay",
            worktree=owner.project,
            session_dir=owner.session.parent,
            session_file=owner.session,
            provider="response-local",
            model="fixture",
            thinking_level="off",
            maintenance_root=owner.root,
        )
    path = record_terminal_failure(
        owner.root,
        turn_id="ba2b5029200e49eea84bf0db17d2316c",
        thread="private-unresponsive-initialization",
        event={},
        sequences=(),
        source_error=failed.value,
    )
    diagnostic = json.loads(path.read_text())
    proof = diagnostic["native"]
    assert proof["input_disposition"] == "not_sent"
    assert proof["initialization"]["control_command"]["type"] == "get_state"
    assert proof["initialization"]["control_command"]["id"]
    assert "stderr" in proof
    assert path.stat().st_mode & 0o777 == 0o600
    assert owner.provider.posts == len(owner.saved_inputs()) == 0
    assert children and all(not child.alive() for child in children)


async def test_actual_tracked_waiting_for_startup_cancels_without_prompt(native_backend, monkeypatch):
    owner = native_backend
    # SelectedExecution library callers supply their original root explicitly;
    # they need not be launched with a worker's ambient root environment.
    monkeypatch.delenv("AGENT_COMMS_ROOT", raising=False)
    entered = asyncio.Event()
    original = owner.session.read_bytes()

    class WaitingTracked(TrackedTurnSession):
        async def open_tools(self, custody):
            await super().open_tools(custody)
            assert self.startup.directory == owner.root / "runtime" / "native-startup"
            entered.set()

    with ExitStack() as held:
        for _ in range(NativeStartupAdmission(owner.root).policy.slots):
            lease = NativeStartupAdmission(owner.root)
            await lease.acquire()
            held.callback(lease.release)
        attempt = asyncio.create_task(
            WaitingTracked.execute(
                Path(os.environ["PI_COMPACTION_TEST_PACKAGE"]),
                input_id="974b3edee3ab41bbba62a4f5128d4265",
                prompt="Private input cancelled before native startup, never replay",
                worktree=owner.project,
                session_dir=owner.session.parent,
                session_file=owner.session,
                provider="response-local",
                model="fixture",
                thinking_level="off",
                maintenance_root=owner.root,
            )
        )
        try:
            await asyncio.wait_for(entered.wait(), 5)
            await asyncio.sleep(0)
            assert not attempt.done()
            assert owner.provider.posts == 0
            attempt.cancel()
            with pytest.raises(asyncio.CancelledError):
                await attempt
        finally:
            attempt.cancel()
            await asyncio.gather(attempt, return_exceptions=True)
    assert owner.session.read_bytes() == original
    assert len(owner.saved_inputs()) == owner.provider.posts == 0
