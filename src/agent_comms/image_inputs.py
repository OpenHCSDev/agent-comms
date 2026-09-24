"""Bounded native image inputs shared by ACP decoding and Pi RPC forwarding."""

from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

IMAGE_MIME_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_PROMPT_IMAGES = 8


@dataclass(frozen=True, slots=True)
class ImageInput:
    data: str
    mime_type: str

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

    def to_rpc(self) -> dict[str, str]:
        return {"type": "image", "data": self.data, "mimeType": self.mime_type}


def prompt_images(blocks: Sequence[object]) -> tuple[ImageInput, ...]:
    images = []
    for block in blocks:
        if not isinstance(block, Mapping):
            dump = getattr(block, "model_dump", None)
            if dump is None:
                continue
            block = dump(by_alias=True, exclude_none=True)
        if block.get("type") == "image":
            data, mime = block.get("data"), block.get("mimeType")
        elif block.get("type") == "resource" and isinstance(block.get("resource"), Mapping):
            resource = block["resource"]
            mime = resource.get("mimeType")
            if not isinstance(mime, str) or not mime.startswith("image/"):
                continue
            data = resource.get("blob")
        else:
            continue
        if not isinstance(data, str) or not isinstance(mime, str):
            raise ValueError("Image input requires base64 data and a MIME type.")
        images.append(ImageInput(data, mime))
        if len(images) > MAX_PROMPT_IMAGES:
            raise ValueError("At most 8 image attachments are allowed per prompt.")
        if sum(image.size for image in images) > MAX_IMAGE_BYTES:
            raise ValueError("Image attachments exceed the 4 MiB prompt limit.")
    return tuple(images)
