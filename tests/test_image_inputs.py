"""Image prompts survive ACP decoding, fresh Pi input and queued steering."""

from bus_history import read_history
import base64

import pytest
from acp import RequestError
from acp.schema import ImageContentBlock

from agent_comms.comms import wire
from agent_comms.image_inputs import MAX_IMAGE_BYTES, ImageInput, prompt_images
from agent_comms.native_arguments import GivenArguments, GivenBinary
from delivery_owner_fixture import canonical_agent

PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aDqkAAAAASUVORK5CYII="
IMAGE = {"type": "image", "data": PNG, "mimeType": "image/png"}


def test_standard_acp_image_and_legacy_resource_are_decoded():
    assert prompt_images([ImageContentBlock.model_validate(IMAGE)]) == (
        ImageInput(PNG, "image/png"),
    )
    assert prompt_images(
        [
            {
                "type": "resource",
                "resource": {"uri": "file:///image.png", "blob": PNG, "mimeType": "image/png"},
            }
        ]
    ) == (ImageInput(PNG, "image/png"),)


@pytest.mark.parametrize(
    "blocks",
    [
        [{**IMAGE, "data": "not base64!"}],
        [{**IMAGE, "data": ""}],
        [{**IMAGE, "mimeType": "image/svg+xml"}],
        [IMAGE] * 9,
    ],
)
def test_invalid_images_fail_instead_of_being_dropped(blocks):
    with pytest.raises(ValueError):
        prompt_images(blocks)


def test_combined_image_budget():
    data = base64.b64encode(b"x" * (MAX_IMAGE_BYTES // 2 + 1)).decode()
    with pytest.raises(ValueError):
        prompt_images([{**IMAGE, "data": data}] * 2)


async def make_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "test/model")
    agent = canonical_agent(
        wire(tmp_path / "wire"), agent_bin=GivenBinary("pi-image-stub"), agent_args=GivenArguments(("--model", "test/model",))
    )
    await agent.new_session(cwd=str(tmp_path / "project"), mcp_servers=[])
    return agent






async def test_routed_image_rejected_before_bus_side_effects(tmp_path, monkeypatch):
    agent = await make_agent(tmp_path, monkeypatch)
    try:
        with pytest.raises(RequestError):
            await agent.prompt("project", [{"type": "text", "text": "#all look"}, IMAGE])
        assert not read_history(agent._comms)

    finally:
        agent.sessions.proxies.clear()
        await agent.shutdown()


