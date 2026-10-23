"""Image prompts survive ACP decoding, fresh Pi input and queued steering."""

import base64

import pytest
from acp import RequestError
from acp.schema import (
    BlobResourceContents,
    EmbeddedResourceContentBlock,
    ImageContentBlock,
    PromptRequest,
    TextContentBlock,
)

from agent_comms.comms import wire
from agent_comms.image_inputs import MAX_IMAGE_BYTES, ImageInput, prompt_images
from agent_comms.native_arguments import GivenArguments, GivenBinary
from bus_history import read_history
from delivery_owner_fixture import canonical_agent

PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aDqkAAAAASUVORK5CYII="
IMAGE = ImageContentBlock(type="image", data=PNG, mime_type="image/png")


def test_standard_acp_image_and_embedded_resource_are_projected():
    assert prompt_images(PromptRequest(session_id="images", prompt=[IMAGE])) == (
        ImageInput(PNG, "image/png"),
    )
    resource = EmbeddedResourceContentBlock(
        type="resource",
        resource=BlobResourceContents(uri="file:///image.png", blob=PNG, mime_type="image/png"),
    )
    assert prompt_images(PromptRequest(session_id="images", prompt=[resource])) == (
        ImageInput(PNG, "image/png"),
    )


@pytest.mark.parametrize(
    "blocks",
    [
        [ImageContentBlock(type="image", data="not base64!", mime_type="image/png")],
        [ImageContentBlock(type="image", data="", mime_type="image/png")],
        [ImageContentBlock(type="image", data=PNG, mime_type="image/svg+xml")],
        [IMAGE] * 9,
    ],
)
def test_invalid_images_fail_instead_of_being_dropped(blocks):
    with pytest.raises(ValueError):
        prompt_images(PromptRequest(session_id="images", prompt=blocks))


def test_combined_image_budget():
    data = base64.b64encode(b"x" * (MAX_IMAGE_BYTES // 2 + 1)).decode()
    with pytest.raises(ValueError):
        prompt_images(
            PromptRequest(
                session_id="images",
                prompt=[ImageContentBlock(type="image", data=data, mime_type="image/png")] * 2,
            )
        )


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
            await agent.prompt("project", [TextContentBlock(type="text", text="#all look"), IMAGE])
        assert not read_history(agent._comms)

    finally:
        agent.sessions.proxies.clear()
        await agent.shutdown()
