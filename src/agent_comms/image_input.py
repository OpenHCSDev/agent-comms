"""Bounded image data carried by native prompts and queued input."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
from typing import Annotated, Literal

from . import field_codec

IMAGE_MIME_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
MAX_IMAGE_BYTES = 4 * 1024 * 1024


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
