"""Saved-session launch fences exercised with the actual pinned Pi child."""

import asyncio
import os
from uuid import uuid4

import pytest

from agent_comms.child_process import ChildRetirementCancelled, ChildRetirementError, ChildRetirementFailed
from agent_comms.native_session_reopen import NativeSessionIdentity

pytest_plugins = ("test_backend_native_lifecycle",)


@pytest.mark.parametrize("stage", ["release_io", "reap"])
@pytest.mark.parametrize("fault", [RuntimeError, TimeoutError])
async def test_stop_failure_accounts_for_native_resources_and_retains_uncertainty(
    tmp_path, monkeypatch, stage, fault
):
    from native_backend_fixture import native_backend_fixture
    from agent_comms.native_custody import RetiringNative

    failure = fault("lost process cleanup receipt")
    with pytest.raises(ChildRetirementFailed) as fixture_failure:
        async with native_backend_fixture(tmp_path) as owner:
            assert (await owner.run("Before process cleanup receipt failure"))[-1].ok
            child = owner.persistent.custody.child
            resource = child.resources.enter_context((tmp_path / "child-resource").open("w"))
            calls = []
            if stage == "release_io":
                def fail_release():
                    assert child.proc.retired
                    calls.append(stage)
                    raise failure
                monkeypatch.setattr(child.proc, "_release_retired_io", fail_release)
            else:
                wait = child.proc.wait
                async def fail_reap():
                    await wait()
                    assert child.proc.retired
                    calls.append(stage)
                    raise failure
                monkeypatch.setattr(child.proc, "wait", fail_reap)
            with pytest.raises(ChildRetirementFailed) as stopped:
                await owner.persistent.close_idle()
            assert stopped.value.__cause__ is failure
            assert calls == [stage]
            assert child.proc.retired and not child.proc.cleanup_complete
            assert child.stderr_task.done() and resource.closed
            assert not child.retired
            assert isinstance(owner.persistent.custody, RetiringNative)
            assert owner.persistent.custody.child is child
            assert not owner.persistent.available
            assert owner.provider.posts == len(owner.saved_inputs()) == 1
    # Fixture teardown still joins all other resources, then reports the cached
    # failed process receipt; it never clears/retries the original stop task.
    assert fixture_failure.value.__cause__ is failure
    assert calls == [stage]


async def test_resource_release_failure_retains_exact_native_child(tmp_path):
    from native_backend_fixture import native_backend_fixture
    from agent_comms.native_custody import RetiringNative

    failure = RuntimeError("native resource release failed")
    with pytest.raises(RuntimeError) as fixture_failure:
        async with native_backend_fixture(tmp_path) as owner:
            assert (await owner.run("Before resource cleanup receipt failure"))[-1].ok
            child = owner.persistent.custody.child
            resource = child.resources.enter_context((tmp_path / "child-resource").open("w"))
            def fail_release():
                raise failure
            child.resources.callback(fail_release)
            with pytest.raises(RuntimeError) as stopped:
                await owner.persistent.close_idle()
            assert stopped.value is failure
            retirement = child._resource_retirement
            assert retirement.done() and retirement.exception() is failure
            assert child.proc.cleanup_complete and child.stderr_task.done() and resource.closed
            assert not child.retired
            assert isinstance(owner.persistent.custody, RetiringNative)
            assert owner.persistent.custody.child is child
            with pytest.raises(RuntimeError) as repeated:
                await owner.persistent.close_idle()
            assert repeated.value is failure and child._resource_retirement is retirement
    assert fixture_failure.value is failure


async def test_actual_native_attestation_refuses_foreign_expected_identity(native_backend):
    owner = native_backend
    first = await owner.run("Diagnostic input before attestation mismatch")
    assert first[-1].ok, first[-1]
    previous = owner.persistent.custody.child.proc
    before = owner.session.read_bytes()
    # Corrupt only the expected witness in this disposable fixture. Pi still
    # reports its actual saved identity through the genuine get_state response.
    owner.persistent.custody.identity = NativeSessionIdentity(str(uuid4()), str(owner.session))
    refused = await owner.run("Must remain unsent after witness mismatch")
    assert not refused[-1].ok and refused[-1].reason_code == "session_identity_uncertain"
    assert not previous.alive() and not owner.persistent.available
    assert len(owner.starts) == owner.provider.posts == 1
    assert owner.session.read_bytes() == before
    print("native_attestation_refusal", refused[-1], flush=True)


@pytest.mark.parametrize("changed", ["session", "credentials", "configuration"])
async def test_actual_native_revision_change_retires_child_without_replay(
    native_backend, changed, monkeypatch
):
    owner = native_backend
    first = await owner.run("Diagnostic input before revision change")
    assert first[-1].ok, first[-1]
    previous = owner.persistent.custody.child.proc
    assert previous is not None and previous.alive()
    if changed == "configuration":
        # Select a distinct private configuration resource with the same local
        # endpoint. The retained launch must not own this different resource.
        selected = owner.config.with_name("selected-config")
        selected.mkdir(mode=0o700)
        for name in ("auth.json", "models.json", "settings.json"):
            (selected / name).write_bytes((owner.config / name).read_bytes())
        monkeypatch.setenv("AGENT_COMMS_NATIVE_CONFIG_DIR", str(selected))
        monkeypatch.setenv("PI_CODING_AGENT_DIR", str(selected))
    else:
        path = owner.session if changed == "session" else owner.config / "auth.json"
        stat = path.stat()
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1000000))
    second = await owner.run("New diagnostic input after revision change")
    assert second[-1].ok, second[-1]
    assert len(owner.children) == 2 and owner.persistent.custody.child.proc is not previous
    assert not previous.alive()
    assert len(owner.starts) == len(owner.saved_inputs()) == owner.provider.posts == 2
    print("native_revision_retirement", changed, second[-1], flush=True)


async def test_cancelled_retirement_joins_exact_child_before_new_borrow(
    native_backend, monkeypatch
):
    import asyncio
    from agent_comms.native_custody import PiSessionChild, RetiringNative

    owner = native_backend
    assert (await owner.run("Before cancelled retirement"))[-1].ok
    previous = owner.persistent.custody.child.proc
    entered, release = asyncio.Event(), asyncio.Event()
    close = PiSessionChild.close

    async def held_close(child):
        if child.proc is previous:
            entered.set()
            await release.wait()
        await close(child)

    monkeypatch.setattr(PiSessionChild, "close", held_close)
    retire = asyncio.create_task(owner.force_reopen())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        retire.cancel()
        with pytest.raises(asyncio.CancelledError):
            await retire
        assert isinstance(owner.persistent.custody, RetiringNative)
        retained_cleanup = owner.persistent.custody.task
        assert previous.alive() and not retained_cleanup.done()
        next_turn = asyncio.create_task(owner.run("New explicit input after retirement"))
        try:
            await asyncio.sleep(0.05)
            assert len(owner.children) == owner.provider.posts == 1
            assert owner.persistent.custody.task is retained_cleanup
            release.set()
            result = await next_turn
            assert result[-1].ok, result[-1]
            assert not previous.alive()
            assert len(owner.children) == owner.provider.posts == len(owner.saved_inputs()) == 2
        finally:
            release.set()
            if not next_turn.done():
                next_turn.cancel()
            await asyncio.gather(next_turn, return_exceptions=True)
    finally:
        release.set()
        await asyncio.gather(retire, return_exceptions=True)


@pytest.mark.parametrize("failure", [TimeoutError, ChildRetirementCancelled])
@pytest.mark.parametrize("already_done", [False, True])
async def test_retired_native_cleanup_failure_is_reported_once_without_poisoning_new_input(
    native_backend, monkeypatch, failure, already_done
):
    """Inject a cleanup IO fault after the real pinned child has actually exited."""
    from agent_comms.native_custody import PiSessionChild

    owner = native_backend
    assert (await owner.run("Before retirement IO failure"))[-1].ok
    previous = owner.persistent.custody.child.proc
    close = PiSessionChild.close

    async def failed_pipe_join(child):
        await close(child)
        if child.proc is previous:
            assert not child.proc.alive()
            raise failure("retirement diagnostic")

    monkeypatch.setattr(PiSessionChild, "close", failed_pipe_join)
    with pytest.raises(failure) as caught:
        if already_done:
            retiring = owner.persistent.custody.retire()
            await asyncio.wait((retiring.task,))  # Complete without consuming its original result.
            await owner.persistent._finish_retirement(retiring)
        else:
            await owner.persistent.close_idle()
    if failure is ChildRetirementCancelled:
        assert isinstance(caught.value, ChildRetirementError)
        assert not isinstance(caught.value, Exception)
    assert not previous.alive()
    # This is the same operation the model control performs. It must not join
    # a failed task which still claims custody over an already retired child.
    await owner.persistent.close_idle()
    result = await owner.run("Distinct new input after retirement IO failure")
    assert result[-1].ok, result[-1]
    assert owner.provider.posts == len(owner.saved_inputs()) == 2
    assert owner.persistent.custody.child.proc is not previous
