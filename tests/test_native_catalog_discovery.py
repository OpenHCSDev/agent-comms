"""Catalogs through actual pinned Pi, without a provider prompt or saved input."""

import asyncio
import json
from dataclasses import replace

import pytest

from agent_comms import backend
from agent_comms.child_process import AttachedChild, BoundedRun
from agent_comms.comms import Comms
from agent_comms.config_options import ModelConfigOption, ThinkingLevelConfigOption
from agent_comms.native_pi import NativePiRpcLaunch, NativePiUnavailable
from agent_comms.pi_commands import SetModel, SetThinkingLevel
from agent_comms.pi_events import Response
from agent_comms.pi_native_backend import PersistentPiSession
from agent_comms.pi_rpc import PiRpcChannel
from agent_comms.threads import Thread
from delivery_owner_fixture import canonical_agent

pytest_plugins = ("test_backend_native_lifecycle",)


async def test_actual_selected_launch_custody_rebuilds_source_and_auth(native_backend, monkeypatch):
    """Actual SDK fork/two fresh children; lend artifact, never input readiness."""
    import hashlib
    import os
    import threading
    from contextlib import AsyncExitStack
    from functools import partial
    from pathlib import Path

    from agent_comms import native_pi
    from agent_comms.coordinated_runtime import SelectedExecution
    from agent_comms.coordination_errors import IdentityConflict
    from agent_comms.coordinator import Coordination
    from agent_comms.native_custody import PiSessionChild
    from agent_comms.native_fork import ForkSessionHelper, ForkSessionRequest
    from agent_comms.native_entries import NativeEntry
    from agent_comms.native_session_reopen import NativeSessionIdentity
    from agent_comms.selected_session import SavedSelectedSession

    source_name = os.environ.get("PI_LAUNCH_TEST_SOURCE")
    if not source_name:
        pytest.skip("Set PI_LAUNCH_TEST_SOURCE to an actual completed native source")
    native = native_backend
    source = Path(source_name).resolve(strict=True)
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    package = Path(os.environ["PI_COMPACTION_TEST_PACKAGE"])
    created = await ForkSessionHelper.run(ForkSessionRequest(
        str(package), str(source), str(native.project),
        directory=str(native.project / "launch-sessions"),
    ), cwd=native.project)
    session = SavedSelectedSession(created.path.parent,
        identity=NativeSessionIdentity(created.session_id, created.session_file))
    execution = SelectedExecution(root=native.root,
        wire_root_id=os.environ["AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID"],
        owner_name="launch-custody", native_package=package)
    verifications = []
    verify = native_pi._trusted_package

    def observed(artifact):
        verifications.append(artifact)
        return verify(artifact)

    monkeypatch.setattr(native_pi, "_trusted_package", observed)
    await Coordination.run_worker(execution.validate)
    launches, children = [], []
    for stage in range(2):
        if stage:
            # A new stage consumes current auth/config, not the old launch key.
            (native.config / "auth.json").write_text(json.dumps({
                "response-local": {"type": "api_key", "key": "changed-local-only"},
            }))
        launch = await Coordination.run_worker(partial(execution.tracked_launch,
            package, worktree=native.project, session=session,
            provider="response-local", model="fixture", thinking_level="off"))
        authentication = launch.configuration.auth_revision()
        launches.append((launch, authentication))
        child = await PiSessionChild.start((launch, authentication), session.attestation())
        children.append(child)
        try:
            async with asyncio.timeout(20):
                pending = child.attestation
                response = await pending.request.exchange(child.reader, child.proc.stdin, strict=True)
                pending.accept(response).require_identity().require_same_session(session.identity)
        finally:
            await child.close()
        assert child.proc.retired and await child.stderr_task == ""
    assert len(verifications) == 1  # both stages consume the pre-claim acquisition
    assert launches[0][1] != launches[1][1]
    assert children[0].proc.identity != children[1].proc.identity
    with pytest.raises(IdentityConflict, match="differs from its execution package"):
        await Coordination.run_worker(partial(execution.tracked_launch,
            package.parent,
            worktree=native.project, session=session,
            provider="response-local", model="fixture"))
    malformed = native.project / "malformed.jsonl"
    malformed.write_text("{}\n")
    wrong = SavedSelectedSession(malformed.parent,
        identity=NativeSessionIdentity(created.session_id, str(malformed)))
    with pytest.raises((ValueError, native_pi.NativePiUnavailable)):
        await Coordination.run_worker(partial(execution.tracked_launch,
            package, worktree=native.project, session=wrong,
            provider="response-local", model="fixture"))
    # Independent new acquisition still verifies the real artifact.
    await Coordination.run_worker(partial(NativePiRpcLaunch.tracked,
        package, worktree=native.project, session=session,
        provider="response-local", model="fixture"))
    assert len(verifications) == 2 and native.provider.posts == 0

    # The same installed worker joins a borrowed, actual SDK-source descriptor.
    # This checks read custody, not a synthetic input or provider commitment.
    entered, release = threading.Event(), threading.Event()
    readers = []

    async def observe_saved_source():
        async with AsyncExitStack() as custody:
            evidence = custody.enter_context(NativeEntry.open_input_evidence(session.path))
            readers.append(evidence)

            def observe():
                evidence.observe()
                entered.set()
                assert release.wait(5), "Original read was not released"
                assert not evidence.source.stream.closed

            await Coordination.run_worker(observe)

    observing = asyncio.create_task(observe_saved_source())
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        for _ in range(2):
            observing.cancel()
            await asyncio.sleep(0)
            assert not observing.done() and not readers[0].source.stream.closed
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await observing
        assert readers[0].source.stream.closed and not readers[0].entries
    finally:
        release.set()
        if not observing.done():
            observing.cancel()
        await asyncio.gather(observing, return_exceptions=True)

    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    print("actual_selected_launch_custody", json.dumps({
        "preclaim_and_first_verifications": 1, "borrowed_verifications": 0,
        "independent_verifications": 1, "auth_changed": True,
        "children_retired": [child.proc.retired for child in children],
        "children": [{"identity": {"pid": child.proc.identity.pid,
                                     "start_time": child.proc.identity.start_time},
                      "alive": child.proc.alive(),
                      "group_members": [member.pid for member in
                          child.proc.platform.group_members(child.proc.identity)]}
                     for child in children],
        "cancelled_saved_read_joined": readers[0].source.stream.closed,
        "source_bytes": source.stat().st_size, "source_sha256": before,
        "provider_requests": native.provider.posts,
    }), flush=True)


@pytest.fixture
async def catalog_owner(native_backend, monkeypatch):
    native = native_backend
    monkeypatch.delenv("AGENT_COMMS_AGENT_MODELS", raising=False)
    children = []
    start = AttachedChild.start

    async def observe(*args, **kwargs):
        child = await start(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(AttachedChild, "start", observe)
    owner = canonical_agent(
        Comms(native.root),
        agent_bin="pi",
        agent_args=["--model", "response-local/fixture", "--offline"],
        auto_wake=False,
    )
    monkeypatch.setattr(owner.inputs, "ensure_live_drain", lambda _: None)
    before = native.session.read_bytes()
    try:
        yield owner, children
    finally:
        await owner.shutdown()
        assert children and all(not child.alive() for child in children)
        assert native.provider.posts == 0
        assert native.session.read_bytes() == before
        assert native.saved_inputs() == []


async def test_actual_catalog_auth_refresh_preserves_selection_and_reaps_children(
    catalog_owner,
    native_backend,
):
    owner, children = catalog_owner
    native = native_backend
    config = owner.sessions.config
    session = await owner.new_session(str(native.project))
    thread = owner._comms.registry.require(session.session_id)
    model_catalog = config.catalog_for(ModelConfigOption)
    thinking_catalog = config.catalog_for(ThinkingLevelConfigOption)
    models = await model_catalog.choices(thread)
    assert "response-local/fixture" in {model.value for model in models}
    assert [choice.value for choice in await thinking_catalog.choices(thread)] == ["off"]
    assert len(children) == 2 and all(not child.alive() for child in children)
    # A cache hit does not launch another native process.
    assert await model_catalog.choices(thread) is models
    assert len(children) == 2
    auth = native.config / "auth.json"
    auth.write_text("{}\n")
    with pytest.raises(NativePiUnavailable, match="Configured model .* is absent from its native catalog"):
        await model_catalog.describe(thread)
    assert len(children) == 3
    assert owner._comms.registry.require(session.session_id).model == "response-local/fixture"
    # Restore this fixture's credential and add another local model. Discovery
    # must read actual updated config instead of an in-process mocked catalog.
    path = native.config / "models.json"
    document = json.loads(path.read_text())
    document["providers"]["response-local"]["models"].append(
        {"id": "second", "name": "Second local model", "reasoning": True}
    )
    path.write_text(json.dumps(document))
    auth.write_text(json.dumps({"response-local": {"type": "api_key", "key": "local-only"}}))
    refreshed = await model_catalog.choices(thread)
    assert {model.value for model in refreshed if model.value.startswith("response-local/")} == {
        "response-local/fixture",
        "response-local/second",
    }
    assert "medium" in {
        choice.value
        for choice in await thinking_catalog.choices(replace(thread, model="response-local/second"))
    }
    assert owner._comms.registry.require(session.session_id).model == "response-local/fixture"
    assert all(not child.alive() for child in children)
    print(f"actual_catalog_children_reaped={len(children)} provider_calls={native.provider.posts}")


async def test_actual_catalog_larger_than_stream_buffer(catalog_owner, native_backend):
    owner, children = catalog_owner
    path = native_backend.config / "models.json"
    document = json.loads(path.read_text())
    models = document["providers"]["response-local"]["models"]
    models.extend({"id": f"large-{i}-" + "x" * 2048, "name": str(i)} for i in range(70))
    path.write_text(json.dumps(document))
    thread = Thread(
        "catalog", frozenset(), str(native_backend.project), model="response-local/fixture"
    )
    result = await owner.sessions.config.catalog_for(ModelConfigOption).choices(thread)
    assert len([model for model in result if model.value.startswith("response-local/")]) == 71
    assert sum(len(model.value) for model in result) > 2 * 65536
    assert len(children) == 1 and not children[0].alive()
    print(f"actual_catalog_identifier_bytes={sum(len(model.value) for model in result)}")


async def test_actual_catalog_cancellation_reaps_child(catalog_owner, native_backend, monkeypatch):
    owner, children = catalog_owner
    reading = asyncio.Event()
    receive = PiRpcChannel.receive

    async def pause_read(channel, **options):
        reading.set()
        await asyncio.Event().wait()
        return await receive(channel, **options)

    monkeypatch.setattr(PiRpcChannel, "receive", pause_read)
    thread = Thread("catalog", frozenset(), str(native_backend.project))
    task = asyncio.create_task(owner.sessions.config.catalog_for(ModelConfigOption).choices(thread))
    try:
        await asyncio.wait_for(reading.wait(), 10)
        assert len(children) == 1 and children[0].alive()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not children[0].alive()


@pytest.mark.refactor_guard
def test_backend_no_longer_owns_discovery_or_catalog_projection():
    assert not {"discover_models", "discover_thinking_levels", "Model"}.intersection(vars(backend))
    assert not hasattr(PiRpcChannel, "request")


async def test_actual_native_setting_responses_use_the_command_result_owner(
    catalog_owner,
    native_backend,
):
    _owner, children = catalog_owner
    launch = NativePiRpcLaunch.managed(
        "pi",
        (
            "--model",
            "response-local/fixture",
            "--offline",
            "--no-session",
            "--no-extensions",
            "--no-skills",
            "--no-context-files",
        ),
        worktree=native_backend.project,
    )
    # The command result owner needs a session; this probe never opens its child.
    session = backend.TurnSession(launch, "", native_session=PersistentPiSession())
    observed = []
    async with BoundedRun.session(launch.argv, cwd=launch.cwd, env=launch.env, timeout=10) as child:
        channel = PiRpcChannel(child.stdout)
        for command, expected in (
            (SetModel(id="model", provider="response-local", model_id="fixture"), True),
            (SetThinkingLevel(id="thinking", level="off"), True),
            (SetModel(id="refused", provider="response-local", model_id="missing"), False),
        ):
            pending = channel.track(command)
            child.stdin.write(channel.command_bytes(command))
            await child.stdin.drain()
            while not pending.done():
                event = await channel.receive()
                assert event is not None
                if isinstance(event, Response):
                    channel.correlate(event)
            response = pending.result()
            updates = [event async for event in command.on_response(response, session)]
            assert len(updates) == 1 and isinstance(updates[0], command.result_type)
            assert updates[0].id == command.id and updates[0].ok is expected
            if not expected:
                assert response.error and updates[0].error == response.error
            observed.append((command.declared_name, updates[0].ok))
        assert not channel.pending._pending
    assert len(children) == 1 and not children[0].alive()
    print("actual_native_setting_results", observed)


@pytest.mark.parametrize("driver", ["immutable_cli", "sdk_callbacks"])
async def test_actual_native_replacement_callback_once_and_cancel(native_backend, monkeypatch, driver):
    """Actual SDK RPC replacement, extension hooks and retirement; no prompt."""
    from functools import partial
    from uuid import uuid4

    from agent_comms.coordinator import Coordination
    from agent_comms.native_custody import PiSessionChild
    import hashlib
    import os
    from pathlib import Path

    from agent_comms.native_fork import ForkSessionHelper, ForkSessionRequest
    from agent_comms.pi_commands import GetState, UnknownCommand

    native = native_backend
    source_name = os.environ.get("PI_REPLACEMENT_TEST_SOURCE")
    if not source_name:
        pytest.skip("Set PI_REPLACEMENT_TEST_SOURCE to an actual completed saved native session")
    source = Path(source_name).resolve(strict=True)
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    created = await ForkSessionHelper.run(ForkSessionRequest(
        os.environ["PI_COMPACTION_TEST_PACKAGE"], str(source), str(native.project),
        directory=str(native.project / "replacement-sessions"),
    ), cwd=native.project)
    events = native.project / "replacement-events.jsonl"
    cancelled_target = native.project / "cancelled.jsonl"
    command = "pi"
    if driver == "sdk_callbacks":
        from native_event_host import install_event_host
        from urllib.parse import urlsplit

        address = urlsplit(json.loads((native.config / "models.json").read_text())["providers"]["response-local"]["baseUrl"])
        command = "sdk-replacement-probe"
        install_event_host(monkeypatch, command, f"{address.scheme}://{address.netloc}",
                           replacement_probe=events, cancelled_session=cancelled_target)
    launch = await Coordination.run_worker(partial(NativePiRpcLaunch.managed,
        command, ("--provider", "response-local", "--model", "fixture", "--thinking", "off",
               "--offline", "--no-extensions",
               "--no-skills", "--no-context-files", "--no-prompt-templates", "--no-tools"),
        worktree=native.project, session_file=created.session_file,
    ))

    def observed():
        return [json.loads(row) for row in events.read_text().splitlines()]

    async with BoundedRun.session(launch.argv, cwd=launch.cwd, env=launch.env, timeout=30) as child:
        channel = PiRpcChannel(child.stdout)
        errors = asyncio.create_task(PiSessionChild.stderr_tail(child.stderr))

        async def state():
            request = GetState(id=uuid4().hex)
            try:
                response = await request.exchange(channel, child.stdin, strict=True)
            except EOFError as error:
                error.add_note(await errors)
                raise
            assert response.responds_to(request) and response.success is True, response
            return response.data.require_payload()

        async def replacement(frame):
            # These are vendor RPC frames, including fork's external entryId.
            # The original open-command representation and channel decode own
            # framing; this control creates no replacement pending registry.
            identity = uuid4().hex
            command = UnknownCommand(wire={"id": identity, **frame})
            child.stdin.write(channel.command_bytes(command))
            await child.stdin.drain()
            while True:
                event = await channel.receive(strict=True)
                assert event is not None, await errors
                if isinstance(event, Response) and event.id == identity:
                    assert event.success, event
                    return event.data.require_payload()

        initial = await state()
        if driver == "sdk_callbacks":
            assert len(observed()) == 1 and observed()[0]["sessionId"] == initial.session_id
        # Fork-before requires an actual user message, unlike clone-at. Ask
        # the active SDK owner, not the raw saved-file tail, for that operand.
        messages = await replacement({"type": "get_fork_messages"})
        entry = messages.payload["messages"][-1]["entryId"]
        for frame in (
            {"type": "switch_session", "sessionPath": created.session_file},
            {"type": "clone"},
            {"type": "fork", "entryId": entry},
            {"type": "new_session"},
        ):
            previous = await state()
            if driver == "sdk_callbacks":
                count = len(observed())
            await replacement(frame)
            current = await state()
            assert current.session_id
            if frame["type"] == "switch_session":
                assert current.identity == previous.identity
            else:
                assert current.session_id != previous.session_id
            if driver == "sdk_callbacks":
                assert len(observed()) == count + 1, (frame, observed())
                assert observed()[-1]["sessionId"] == current.session_id
        if driver == "sdk_callbacks":
            before = await state()
            count = len(observed())
            await replacement({"type": "switch_session", "sessionPath": str(cancelled_target)})
            after = await state()
            assert after.identity == before.identity and len(observed()) == count
            assert not cancelled_target.exists()
        assert native.provider.posts == 0 and native.saved_inputs() == []
        assert hashlib.sha256(source.read_bytes()).hexdigest() == source_hash
        print("actual_native_replacement", driver, observed() if driver == "sdk_callbacks" else current.session_id, flush=True)
    assert child.returncode is not None and not child.alive()
    assert await errors == ""
