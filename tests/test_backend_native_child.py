"""Saved-session launch fences exercised with the actual pinned Pi child."""

import asyncio

import pytest

from agent_comms.child_process import ChildRetirementCancelled, ChildRetirementError


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
