"""Actual Pi terminal evidence, no-tool authority and retained image privacy."""

import json

import pytest

import agent_comms.agent_events as events
from agent_comms.image_inputs import ImageInput



async def test_actual_native_cold_preparation_retains_without_admitting_input(native_backend):
    owner = native_backend
    observed = []

    async def observe(thread, state, info):
        observed.append((state, info))

    async with owner.open_owner() as (agent, session_id):
        native = agent.turns.persistent_backends[session_id]
        await native.close()
        native.observe = observe
        thread = agent._comms.registry.require(agent.sessions.require(session_id))
        state = await native.prepare(thread)
        owner.children.append(native.child_state.child.proc)
        assert observed == [(state, observed[0][1])]
        assert state.session_file == str(owner.session) and state.session_id
        assert state.is_streaming is False and state.is_compacting is False
        assert native.available and native.child_state.child.proc.alive()
        assert len(owner.starts) == len(owner.saved_inputs()) == owner.provider.posts == 0


async def test_actual_no_tool_turn_rejects_provider_tool_call(native_backend, monkeypatch):
    owner = native_backend
    ordinary = owner.provider.response_chunks

    def unavailable_then_answer():
        if owner.provider.posts == 1:
            yield {'tool_calls': [{'index': 0, 'id': 'unavailable-tool', 'type': 'function',
                   'function': {'name': 'unavailable_tool', 'arguments': '{}'}}]}, None
            yield {}, 'tool_calls'
        else:
            yield from ordinary()

    monkeypatch.setattr(owner.provider, 'response_chunks', unavailable_then_answer)
    from agent_comms.native_tool_call import SelectedToolDenied

    with pytest.raises(SelectedToolDenied, match='Native tools are disabled'):
        await owner.run('No model tool is authorized for this input')
    assert len(owner.starts) == len(owner.saved_inputs()) == owner.provider.posts == 1
    assert not owner.persistent.available
    assert all(not child.alive() for child in owner.children)


async def test_actual_native_retained_image_failure_redacts_provider_text(native_backend):
    owner = native_backend
    model_file = owner.config / "models.json"
    config = json.loads(model_file.read_text())
    config["providers"]["response-local"]["models"][0]["input"] = ["text", "image"]
    model_file.write_text(json.dumps(config))
    image = ImageInput(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jZ1kAAAAASUVORK5CYII=",
        "image/png",
    )
    first = await owner.run("Inspect this diagnostic image", images=(image,))
    assert first[-1].ok, first[-1]
    retained = owner.persistent.child_state.child.proc
    assert retained is not None and owner.persistent.child_state.child.sensitive_diagnostics
    owner.provider.status = 400
    owner.provider.error_type = 'invalid_request_error'
    owner.provider.error_message = 'loopback retryable failure'
    second = await owner.run("A new diagnostic input after the image")
    assert not second[-1].ok
    assert second[-1].text == "Image prompt failed; backend diagnostics withheld."
    assert all(
        event.text == second[-1].text and not event.diagnostics
        for event in second
        if isinstance(event, events.Error)
    )
    assert "loopback retryable failure" not in repr(second)
    assert len(owner.starts) == len(owner.saved_inputs()) == owner.provider.posts == 2
    assert len(owner.children) == 1 and not retained.alive()
    print("native_image_failure", repr(second), flush=True)
