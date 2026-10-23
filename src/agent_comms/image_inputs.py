"""Bounded native image inputs shared by ACP decoding and Pi RPC forwarding."""

from __future__ import annotations

from agent_comms.wire_models import WireAdapter

import base64
import binascii
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Annotated, Literal
from agent_comms import field_codec
from acp.schema import (
    BlobResourceContents,
    EmbeddedResourceContentBlock,
    ImageContentBlock,
    PromptRequest,
    TextResourceContents,
)
from pydantic import TypeAdapter
from .mro_dispatch import MroDispatch, handles

IMAGE_MIME_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_PROMPT_IMAGES = 8


@dataclass(frozen=True, slots=True)
class ImageInput:
    data: str
    mime_type: Annotated[str, field_codec.WireName("mimeType")]
    type: Annotated[Literal["image"], field_codec.WireRequired()] = field(
        default="image", kw_only=True
    )

    def __post_init__(self) -> None:
        if self.mime_type not in IMAGE_MIME_TYPES:
            raise ValueError(f"Unsupported image type: {self.mime_type}")
        if len(self.data) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
            raise ValueError("Image attachments exceed the 4 MiB prompt limit.")
        try:
            decoded = base64.b64decode(self.data, validate=True)
        except (ValueError, binascii.Error) as error:
            raise ValueError("Image data must be valid base64.") from error
        if not decoded:
            raise ValueError("Image data is empty.")
        if len(decoded) > MAX_IMAGE_BYTES:
            raise ValueError("Image attachments exceed the 4 MiB prompt limit.")

    @property
    def size(self) -> int:
        return len(self.data) * 3 // 4 - (len(self.data) - len(self.data.rstrip("=")))


class PromptImages(MroDispatch):
    """Project ACP's declared content family into bounded native images."""

    def __init__(self) -> None:
        self.images: list[ImageInput] = []

    def append(self, image: ImageInput) -> None:
        self.images.append(image)
        if len(self.images) > MAX_PROMPT_IMAGES:
            raise ValueError("At most 8 image attachments are allowed per prompt.")
        if sum(image.size for image in self.images) > MAX_IMAGE_BYTES:
            raise ValueError("Image attachments exceed the 4 MiB prompt limit.")

    @handles(ImageContentBlock)
    def image(self, block: ImageContentBlock) -> None:
        self.append(ImageInput(block.data, block.mime_type))

    @handles(EmbeddedResourceContentBlock)
    def resource(self, block: EmbeddedResourceContentBlock) -> None:
        self.dispatch_sync(block.resource)

    @handles(BlobResourceContents)
    def blob(self, resource: BlobResourceContents) -> None:
        if resource.mime_type and resource.mime_type.startswith("image/"):
            self.append(ImageInput(resource.blob, resource.mime_type))

    @handles(TextResourceContents)
    def text(self, resource: TextResourceContents) -> None:
        if resource.mime_type and resource.mime_type.startswith("image/"):
            raise ValueError("Image input requires base64 data and a MIME type.")


_PROMPT_CONTENT: TypeAdapter[list[object]] = WireAdapter(
    PromptRequest.model_fields["prompt"].rebuild_annotation()
)


def prompt_images(blocks: Sequence[object]) -> tuple[ImageInput, ...]:
    images = PromptImages()
    for block in _PROMPT_CONTENT.validate_python(list(blocks), strict=True):
        images.dispatch_sync(block)
    return tuple(images.images)
