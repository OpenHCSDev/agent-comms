"""Catalogs through actual pinned Pi, without a provider prompt or saved input."""

import asyncio
import json
from dataclasses import replace

import pytest

from agent_comms import backend
from agent_comms.child_process import AttachedChild, BoundedRun
from agent_comms.comms import Comms
from agent_comms.config_options import ModelConfigOption, ThinkingLevelConfigOption
from agent_comms.native_pi import NativePiRpcLaunch
from agent_comms.pi_commands import SetModel, SetThinkingLevel
from agent_comms.pi_events import Response
from agent_comms.pi_rpc import PiRpcChannel
from agent_comms.threads import Thread
from delivery_owner_fixture import canonical_agent

pytest_plugins = ("test_backend_native_lifecycle",)


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
    unavailable = await model_catalog.describe(thread)
    assert unavailable.current_value == "response-local/fixture"
    assert "response-local/fixture" in {choice.value for choice in unavailable.options}
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
    session = backend.TurnSession(launch, "")
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


async def test_actual_native_replacement_callback_once_and_cancel(native_backend):
    """Actual SDK RPC replacement, extension hooks and retirement; no prompt."""
    from uuid import uuid4

    from agent_comms.native_entries import NativeEvidenceRead
    from agent_comms.pi_commands import GetState, UnknownCommand

    native = native_backend
    events = native.project / "replacement-events.jsonl"
    cancelled_target = native.project / "cancelled.jsonl"
    extension = native.project / "replacement-observer.mjs"
    extension.write_text(
        "import {appendFileSync} from 'node:fs';\n"
        "export default function(pi) {\n"
        "  pi.on('session_start', (event,ctx) => appendFileSync("
        + json.dumps(str(events))
        + ",JSON.stringify({event:'session_start',reason:event.reason,"
        "sessionId:ctx.sessionManager.getSessionId()})+'\\n'));\n"
        "  pi.on('session_before_switch', (event) => {\n"
        "    if (event.targetSessionFile === "
        + json.dumps(str(cancelled_target))
        + ") return {cancel:true};\n"
        "  });\n"
        "}\n"
    )
    launch = NativePiRpcLaunch.managed(
        "pi", ("--model", "response-local/fixture", "--thinking", "off",
               "--offline", "--no-extensions", "--extension", str(extension),
               "--no-skills", "--no-context-files", "--no-prompt-templates", "--no-tools"),
        worktree=native.project, session_file=str(native.session),
    )

    def observed():
        return [json.loads(row) for row in events.read_text().splitlines()]

    async with BoundedRun.session(launch.argv, cwd=launch.cwd, env=launch.env, timeout=30) as child:
        channel = PiRpcChannel(child.stdout)

        async def state():
            request = GetState(id=uuid4().hex)
            return (await request.exchange(channel, child.stdin, strict=True)).require_request(request)

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
                assert event is not None
                if isinstance(event, Response) and event.id == identity:
                    assert event.success, event
                    return

        initial = await state()
        assert len(observed()) == 1 and observed()[0]["sessionId"] == initial.session_id
        # Native startup's real metadata leaf suffices for SDK fork/clone;
        # there is no fabricated user/assistant seed or provider invocation.
        with NativeEvidenceRead.open(native.session) as evidence:
            _, entries = evidence.observe()
            leaf = entries[-1].require_entry_id()
        for frame in (
            {"type": "switch_session", "sessionPath": str(native.session)},
            {"type": "fork", "entryId": leaf},
            {"type": "clone"},
            {"type": "new_session"},
        ):
            count = len(observed())
            await replacement(frame)
            current = await state()
            assert len(observed()) == count + 1, (frame, observed())
            assert observed()[-1]["sessionId"] == current.session_id
        before = await state()
        count = len(observed())
        await replacement({"type": "switch_session", "sessionPath": str(cancelled_target)})
        after = await state()
        assert after.identity == before.identity and len(observed()) == count
        assert not cancelled_target.exists()
        assert native.provider.posts == 0 and native.saved_inputs() == []
        print("actual_native_replacement_hooks", observed(), flush=True)
    assert child.returncode is not None and not child.alive()
