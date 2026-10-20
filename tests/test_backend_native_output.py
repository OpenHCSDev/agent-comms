"""Actual Pi output ownership, including tool recovery and retained image privacy."""

import asyncio
import json
import os

import agent_comms.agent_events as events
from agent_comms.image_inputs import ImageInput
from agent_comms.native_session_prepare import NativeSessionPreparation


async def test_actual_native_cold_preparation_retains_without_admitting_input(native_backend):
    owner = native_backend
    observed = []

    async def observe(state, info):
        observed.append((state, info))

    state = await NativeSessionPreparation.open(
        owner.persistent,
        "pi",
        [
            "--provider",
            "response-local",
            "--model",
            "fixture",
            "--offline",
            "--no-extensions",
            "--no-tools",
        ],
        package=owner.package,
        worktree=str(owner.project),
        root=owner.root,
        session_directory=owner.root,
        environment=dict(os.environ),
        session_file=str(owner.session),
        observe=observe,
    )
    assert observed == [(state, observed[0][1])]
    assert state.session_file == str(owner.session) and state.session_id
    assert state.is_streaming is False and state.is_compacting is False
    assert owner.persistent.available and owner.persistent.child_state.child.proc.alive()
    assert len(owner.starts) == len(owner.saved_inputs()) == owner.provider.posts == 0


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
    owner.provider.status = 503
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
