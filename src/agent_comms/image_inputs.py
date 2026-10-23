"""Bounded native image inputs shared by ACP decoding and Pi RPC forwarding."""

from __future__ import annotations

from acp.schema import (
    BlobResourceContents,
    EmbeddedResourceContentBlock,
    ImageContentBlock,
    PromptRequest,
    TextResourceContents,
)

from . import image_input
from .mro_dispatch import MroDispatch, handles

MAX_PROMPT_IMAGES = 8


class PromptImages(MroDispatch):
    """Project ACP's declared content family into bounded native images."""

    def __init__(self) -> None:
        self.images: list[image_input.ImageInput] = []

    def append(self, image: image_input.ImageInput) -> None:
        self.images.append(image)
        if len(self.images) > MAX_PROMPT_IMAGES:
            raise ValueError("At most 8 image attachments are allowed per prompt.")
        if sum(image.size for image in self.images) > image_input.MAX_IMAGE_BYTES:
            raise ValueError("Image attachments exceed the 4 MiB prompt limit.")

    @handles(ImageContentBlock)
    def image(self, block: ImageContentBlock) -> None:
        self.append(image_input.ImageInput(block.data, block.mime_type))

    @handles(EmbeddedResourceContentBlock)
    def resource(self, block: EmbeddedResourceContentBlock) -> None:
        self.dispatch_sync(block.resource)

    @handles(BlobResourceContents)
    def blob(self, resource: BlobResourceContents) -> None:
        if resource.mime_type and resource.mime_type.startswith("image/"):
            self.append(image_input.ImageInput(resource.blob, resource.mime_type))

    @handles(TextResourceContents)
    def text(self, resource: TextResourceContents) -> None:
        if resource.mime_type and resource.mime_type.startswith("image/"):
            raise ValueError("Image input requires base64 data and a MIME type.")


def prompt_images(request: PromptRequest) -> tuple[image_input.ImageInput, ...]:
    images = PromptImages()
    for block in request.prompt:
        images.dispatch_sync(block)
    return tuple(images.images)
