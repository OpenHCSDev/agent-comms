"""Original successful SDK file operation, not current filesystem authority."""
from dataclasses import dataclass
from pathlib import Path

from .declared_family import DeclaredFamily
from .text_digest import TextDigest


class NativeFileArtifact(DeclaredFamily, affix="FileArtifact"):
    """The original result carries its own declared evidence membership."""


@dataclass(frozen=True)
class Utf8FileWriteArtifact(NativeFileArtifact, declared_name="utf8_file_write"):
    operation_path: str
    digest: TextDigest
    byte_count: int

    def __post_init__(self):
        if not Path(self.operation_path).is_absolute():
            raise ValueError("Original SDK operation requires its addressed absolute path")
        if not 0 <= self.byte_count <= 2**53 - 1:
            raise ValueError("Original SDK operation byte count is invalid")
