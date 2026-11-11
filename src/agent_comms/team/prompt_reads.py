"""Prepared prompt locations and the exact source reads their manifests retain."""
from __future__ import annotations
import hashlib
import json
from dataclasses import dataclass
from importlib.util import find_spec
from pathlib import Path, PurePosixPath

from agent_comms.nominal.field_codec import FieldCodec


@dataclass(frozen=True)
class DealtSource:
    """A dealt file by what it is, never by where an install put it: its package and its path
    inside that package. Every install moves the runtime, so a read keyed by the absolute path
    was a read of a place, and each identical install demanded every file again."""

    package: str
    relative: str

    @classmethod
    def of(cls, path: Path) -> DealtSource:
        """The installed package file this path names, under this runtime or an earlier one."""
        source = cls.find(path)
        if source is None:
            raise ValueError(f"{path} names no file of an installed package")
        return source

    @classmethod
    def find(cls, path: Path) -> DealtSource | None:
        """The current installed source, or none when its package no longer supplies it."""
        for index, name in enumerate(path.parts[:-1]):
            source = cls(name, PurePosixPath(*path.parts[index + 1:]).as_posix())
            if name.isidentifier() and source.installed():
                return source
        return None

    def installed(self) -> bool:
        spec = find_spec(self.package)
        return bool(spec and spec.submodule_search_locations) and self.path.is_file()

    @property
    def path(self) -> Path:
        """Where the running install holds this file."""
        spec = find_spec(self.package)
        assert spec is not None and spec.submodule_search_locations is not None
        return Path(spec.submodule_search_locations[0]) / self.relative

    def digest(self) -> str:
        return hashlib.sha256(self.path.read_bytes()).hexdigest()


@dataclass(frozen=True)
class DealtRead:
    """One dealt file read in full: the source and the content that was read. It still counts
    under any runtime holding that content, and stops counting the moment the file changes."""

    source: DealtSource
    digest: str

    def current(self) -> bool:
        return self.digest == self.source.digest()

    def line(self) -> str:
        """One line of a deal's read log."""
        return json.dumps(FieldCodec.encode(self)) + "\n"


@dataclass(frozen=True)
class PromptReads:
    """The dealt files a system prompt carries, each as a read of the exact content it carries.
    A file changed after the prompt was prepared no longer matches, so it is read like any
    other; nothing is waived for content the session never saw."""

    reads: tuple[DealtRead, ...]


@dataclass(frozen=True)
class PreparedPrompt:
    """One prepared prompt and its adjacent read manifest."""
    prompt: Path

    @property
    def manifest(self) -> Path:
        prompt = self.prompt.resolve()
        return prompt.with_name(prompt.name + ".reads.json")
