"""Saved-session launch fences exercised with the actual pinned Pi child."""

import asyncio
import os
from uuid import uuid4

import pytest

from agent_comms.child_process import ChildRetirementCancelled, ChildRetirementError, ChildRetirementFailed
from agent_comms.native_session_reopen import NativeSessionIdentity



@pytest.mark.parametrize("stage", ["release_io", "reap"])
@pytest.mark.parametrize("fault", [RuntimeError, TimeoutError])
async def test_stop_failure_accounts_for_native_resources_and_retains_uncertainty(
    tmp_path, monkeypatch, stage, fault, native_runtime_package
):
    from native_backend_fixture import native_backend_fixture
    from agent_comms.native_child import RetiringNative

    failure = fault("lost process cleanup acknowledgement")
    with pytest.raises(ChildRetirementFailed) as fixture_failure:
        async with native_backend_fixture(tmp_path, package=native_runtime_package) as owner:
            assert (await owner.run("Before process cleanup acknowledgement failure"))[-1].ok
            child = owner.persistent.child_state.child
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
            assert child.proc.retired
            await child.proc.stderr_tail()
            assert resource.closed
            assert child._close_job.result() is stopped.value
            assert not child.retired
            assert isinstance(owner.persistent.child_state, RetiringNative)
            assert owner.persistent.child_state.child is child
            assert not owner.persistent.available
            assert owner.provider.posts == len(owner.saved_inputs()) == 1
    # Fixture teardown still joins all other resources, then reports the cached
    # failed process acknowledgement; it never clears/retries the original stop task.
    assert fixture_failure.value.__cause__ is failure
    assert calls == [stage]


async def test_resource_release_failure_retains_exact_native_child(tmp_path, native_runtime_package):
    from native_backend_fixture import native_backend_fixture
    from agent_comms.native_child import RetiringNative

    failure = RuntimeError("native resource release failed")
    with pytest.raises(RuntimeError) as fixture_failure:
        async with native_backend_fixture(tmp_path, package=native_runtime_package) as owner:
            assert (await owner.run("Before resource cleanup acknowledgement failure"))[-1].ok
            child = owner.persistent.child_state.child
            resource = child.resources.enter_context((tmp_path / "child-resource").open("w"))
            def fail_release():
                raise failure
            child.resources.callback(fail_release)
            with pytest.raises(RuntimeError) as stopped:
                await owner.persistent.close_idle()
            assert stopped.value is failure
            retirement = child._close_job
            assert retirement.done() and retirement.result() is failure
            assert child.proc.retired and resource.closed
            await child.proc.stderr_tail()
            assert not child.retired
            assert isinstance(owner.persistent.child_state, RetiringNative)
            assert owner.persistent.child_state.child is child
            with pytest.raises(RuntimeError) as repeated:
                await owner.persistent.close_idle()
            assert repeated.value is failure and child._close_job is retirement
    assert fixture_failure.value is failure


@pytest.mark.parametrize("borrowed", [False, True])
async def test_all_native_shutdown_consumers_share_one_complete_job(
    native_backend, monkeypatch, borrowed
):
    from agent_comms.native_child import BorrowedNative, EmptyNative

    owner = native_backend
    assert (await owner.run("Before shared shutdown"))[-1].ok
    child = owner.persistent.child_state.child
    following = EmptyNative()
    if borrowed:
        owner.persistent.child_state = BorrowedNative(child, following)
    entered, release = asyncio.Event(), asyncio.Event()
    stop = child.proc.stop
    calls = []
    async def retire_gateway():
        calls.append("gateway")
    child.transport.push_async_callback(retire_gateway)
    async def counted_stop():
        calls.append("stop")
        outcome = await stop()
        entered.set()
        await release.wait()
        return outcome
    child.resources.callback(calls.append, "resources")
    monkeypatch.setattr(child.proc, "stop", counted_stop)
    async def failure_consumer():
        async with child.failures():
            raise ValueError("original consumer error")
    handler = asyncio.create_task(failure_consumer())
    waiters = [handler]
    try:
        await asyncio.wait_for(entered.wait(), 5)
        operation = child._close_job
        retiring = owner.persistent.child_state.retire()
        assert retiring.child is child and child._close_job is operation
        state_waiter = asyncio.create_task(owner.persistent._finish_retirement(retiring))
        direct_waiter = asyncio.create_task(child.shutdown())
        waiters.extend((state_waiter, direct_waiter))
        release.set()
        with pytest.raises(ValueError, match="original consumer error"):
            await handler
        await state_waiter
        await direct_waiter
        from agent_comms.child_process import ChildOutcome
        assert child._close_job is operation and isinstance(operation.result(), ChildOutcome)
        assert calls == ["gateway", "stop", "resources"]
        assert child.retired
        await child.proc.stderr_tail()
        assert isinstance(owner.persistent.child_state, EmptyNative)
        if borrowed:
            assert owner.persistent.child_state is following
        assert (await owner.run("Distinct input after shared shutdown"))[-1].ok
        assert owner.provider.posts == len(owner.saved_inputs()) == 2
    finally:
        release.set()
        await asyncio.gather(*waiters, return_exceptions=True)


async def test_actual_native_attestation_refuses_foreign_expected_identity(native_backend):
    owner = native_backend
    first = await owner.run("Diagnostic input before attestation mismatch")
    assert first[-1].ok, first[-1]
    previous = owner.persistent.child_state.child.proc
    before = owner.session.read_bytes()
    # Corrupt only the expected witness in this disposable fixture. Pi still
    # reports its actual saved identity through the genuine get_state response.
    owner.persistent.child_state.identity = NativeSessionIdentity(str(uuid4()), str(owner.session))
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
    previous = owner.persistent.child_state.child.proc
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
    assert len(owner.children) == 2 and owner.persistent.child_state.child.proc is not previous
    assert not previous.alive()
    assert len(owner.starts) == len(owner.saved_inputs()) == owner.provider.posts == 2
    print("native_revision_retirement", changed, second[-1], flush=True)


async def test_cancelled_retirement_joins_exact_child_before_new_borrow(
    native_backend, monkeypatch
):
    import asyncio
    from agent_comms.native_child import PiSessionChild, RetiringNative

    owner = native_backend
    assert (await owner.run("Before cancelled retirement"))[-1].ok
    previous = owner.persistent.child_state.child.proc
    entered, release = asyncio.Event(), asyncio.Event()
    close = PiSessionChild._shutdown

    async def held_close(child, *, graceful):
        if child.proc is previous:
            entered.set()
            await release.wait()
        return await close(child, graceful=graceful)

    monkeypatch.setattr(PiSessionChild, "_shutdown", held_close)
    retire = asyncio.create_task(owner.force_reopen())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        retire.cancel()
        assert not retire.done()
        assert isinstance(owner.persistent.child_state, RetiringNative)
        retained_cleanup = owner.persistent.child_state.child._close_job
        assert previous.alive() and not retained_cleanup.done()
        assert owner.persistent.lock.locked()
        next_turn = asyncio.create_task(owner.run("New explicit input after retirement"))
        try:
            assert len(owner.children) == owner.provider.posts == 1
            assert owner.persistent.child_state.child._close_job is retained_cleanup
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await retire
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
@pytest.mark.parametrize("gateway", [False, True])
async def test_failed_owned_shutdown_preserves_original_outcome_without_retry(
    tmp_path, failure, already_done, gateway, native_runtime_package
):
    from native_backend_fixture import native_backend_fixture
    from agent_comms.native_child import RetiringNative

    original_error = failure("owned shutdown failed")
    with pytest.raises(failure) as fixture_failure:
        async with native_backend_fixture(tmp_path, package=native_runtime_package) as owner:
            assert (await owner.run("Before owned shutdown failure"))[-1].ok
            child = owner.persistent.child_state.child
            calls = []
            def failed_resources():
                calls.append("release")
                raise original_error
            if gateway:
                child.transport.callback(failed_resources)
            else:
                child.resources.callback(failed_resources)
            with pytest.raises(failure) as caught:
                if already_done:
                    retiring = owner.persistent.child_state.retire()
                    observer = asyncio.create_task(child.shutdown())
                    await asyncio.gather(observer, return_exceptions=True)
                    await owner.persistent._finish_retirement(retiring)
                else:
                    await owner.persistent.close_idle()
            assert caught.value is original_error
            if failure is ChildRetirementCancelled:
                assert isinstance(caught.value, ChildRetirementError)
                assert not isinstance(caught.value, Exception)
            operation = child._close_job
            assert operation.done() and operation.result() is original_error
            assert not child.retired and child.proc.retired
            await child.proc.stderr_tail()
            assert isinstance(owner.persistent.child_state, RetiringNative)
            with pytest.raises(failure) as repeated:
                await owner.persistent.close_idle()
            assert repeated.value is original_error
            with pytest.raises(failure) as direct:
                await child.shutdown()
            assert direct.value is original_error
            with pytest.raises(failure) as handler:
                async with child.failures():
                    raise ValueError("independent diagnostic failure")
            assert handler.value is original_error
            assert child._close_job is operation and calls == ["release"]
            assert owner.provider.posts == len(owner.saved_inputs()) == 1
    assert fixture_failure.value is original_error
