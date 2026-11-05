"""Stable file-content observations shared by interpreter and wheel build inputs."""

from __future__ import annotations

import hashlib
from io import BufferedReader
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, field_validator

from agent_comms.nominal.field_codec import Sha256Text
from agent_comms.storage.private_path import FileRevision


class BuildArtifact(BaseModel):
    """A native file identity and its bytes, captured through the same open handle."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    path: Path
    revision: FileRevision
    sha256: Annotated[str, Sha256Text]

    @field_validator("sha256")
    @classmethod
    def valid_digest(cls, value: str) -> str:
        return Sha256Text.decode(value)

    @classmethod
    def capture(cls, path: Path) -> BuildArtifact:
        path = path.resolve(strict=True)
        with path.open("rb") as stream:
            return cls.from_stream(stream)

    @classmethod
    def from_stream(cls, stream: BufferedReader) -> BuildArtifact:
        """Capture the already-open file, including a platform-qualified native image."""
        path = Path(stream.name).resolve(strict=True)
        stream.seek(0)
        revision = FileRevision.from_file(stream)
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if revision != FileRevision.from_file(stream):
            raise ValueError(f"Build artifact changed while reading: {path}")
        if FileRevision.capture(path) != revision:
            raise ValueError(f"Build artifact was replaced while reading: {path}")
        return cls(path=path, revision=revision, sha256=digest)
