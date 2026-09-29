"""Catalogs through actual pinned Pi, without a provider prompt or saved input."""

import asyncio
import json
from dataclasses import replace

import pytest

from agent_comms import backend
from agent_comms.child_process import AttachedChild
from agent_comms.comms import Comms
from agent_comms.config_options import ModelConfigOption, ThinkingLevelConfigOption
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
    unavailable = await model_catalog.describe(config, thread)
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
