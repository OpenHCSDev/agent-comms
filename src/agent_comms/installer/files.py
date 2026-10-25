"""Installation file locations derive from their durable declarations."""

from __future__ import annotations

import os
import shutil
import tempfile
from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, ClassVar

from agent_comms.field_codec import PathText

from agent_comms.child_process import Platform
from agent_comms.errors import RelationViolationError
from agent_comms.store_files import AtomicFile
from agent_comms.locked_store import DeclaredFile, RootFile, SQLiteFile, UndeclaredFile
from agent_comms.typed_table import WriterOwnedStore


class InstallationFile(ABC):
    """One file's stable backup name and its location in this installation."""

    @property
    @abstractmethod
    def path(self) -> Path: ...

    @property
    @abstractmethod
    def name(self) -> Path: ...

    @property
    @abstractmethod
    def owner(self) -> type[RootFile]: ...

    def content_files(self) -> dict[Path, Path]:
        return {self.name: self.path} if self.path.exists() else {}

    def content_digest(self, path: Path) -> str:
        return self.owner.digest(path)

    def copy_content(self, source: Path, target: Path) -> None:
        self.owner.copy_file(source, target)

    def capture(self, directory: Path) -> dict[Path, str]:
        if not self.path.exists():
            return {}
        saved = {}
        with self.owner.writers_drained(self.path):
            for name, source in self.content_files().items():
                target = directory / name
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                self.copy_content(source, target)
                Platform.current().sync_file(target)
                saved[name] = self.content_digest(target)
                Platform.current().sync_directory(target.parent)
        return saved

    def saved_names(self, saved: dict[Path, str]) -> frozenset[Path]:
        return frozenset(self.member_name(path) for path in self.owner.files(self.path)) & saved.keys()

    def original(self, saved: dict[Path, str]) -> bool:
        current = self.content_files()
        return (current.keys() == self.saved_names(saved)
                and all(self.content_digest(path) == saved[name] for name, path in current.items()))

    @staticmethod
    def restore_file(source: Path, target: Path) -> None:
        missing = [parent for parent in target.parents if not parent.exists()]
        for parent in reversed(missing):
            parent.mkdir(mode=0o700)
            Platform.current().sync_directory(parent.parent)
        descriptor, temporary = tempfile.mkstemp(prefix=".restore-", dir=target.parent)
        os.close(descriptor)
        stage = Path(temporary)
        try:
            shutil.copy2(source, stage)
            AtomicFile(target).replace(stage)
        finally:
            stage.unlink(missing_ok=True)

    def restore(self, directory: Path, saved: dict[Path, str]) -> None:
        for target in self.owner.files(self.path):
            name = self.member_name(target)
            if name in saved:
                self.restore_file(directory / name, target)
            else:
                target.unlink(missing_ok=True)
        Platform.current().sync_directory(self.path.parent)

    def member_name(self, member: Path) -> Path:
        """Companion files keep their declaring file's relative directory."""
        return self.name.parent / member.relative_to(self.path.parent)

    def require_direct(self) -> None:
        if self.path.resolve() != self.path:
            raise RelationViolationError(f"Installation refuses an aliased file: {self.path}")


@dataclass(frozen=True)
class BusInstallationFile(InstallationFile):
    root: Path
    resource: DeclaredFile | UndeclaredFile
    file_owner: type[RootFile]

    @property
    def path(self) -> Path:
        return self.root / self.name

    @property
    def name(self) -> Path:
        return self.resource.path

    @property
    def owner(self) -> type[RootFile]:
        return self.file_owner


@dataclass(frozen=True)
class WriterOwnedInstallationFile(InstallationFile):
    store: type[WriterOwnedStore]
    directory: ClassVar[Path] = Path("writer-owned")

    @property
    def path(self) -> Path:
        return self.store.path()

    @property
    def name(self) -> Path:
        return self.directory / self.store.declared_name

    @property
    def owner(self) -> type[RootFile]:
        return SQLiteFile

    def content_files(self) -> dict[Path, Path]:
        observed = {path: path.stat() for path in self.owner.files(self.path) if path.is_file()}
        return {self.member_name(path): path
                for path in self.owner.snapshot_members(self.path, observed) if path in observed}

    def content_digest(self, path: Path) -> str:
        return RootFile.digest(path)

    def copy_content(self, source: Path, target: Path) -> None:
        RootFile.copy_file(source, target)

    def member_name(self, member: Path) -> Path:
        """Companions derive their retained name from the primary's declared name."""
        suffix = member.name.removeprefix(self.path.name)
        return self.name.with_name(self.name.name + suffix)


@dataclass(frozen=True)
class CopiedWriterOwnedFile(WriterOwnedInstallationFile):
    root: Path

    @property
    def path(self) -> Path:
        return self.root / self.name


@dataclass(frozen=True)
class SelectedWriterOwnedFile(WriterOwnedInstallationFile):
    """The selected physical path crosses the target-worker boundary with its declaration."""

    selected_path: Annotated[Path, PathText]

    @property
    def path(self) -> Path:
        return self.selected_path


@dataclass(frozen=True)
class InstallationFiles:
    """Resolve physical selection and retained names through the same declarations."""

    root: Path

    @classmethod
    def for_carry(cls, root: Path, original_root: Path) -> InstallationFiles:
        """A copied carry can reach only copies, never the original writer's path."""
        if root == original_root:
            return cls(root)
        return CopiedInstallationFiles(root)

    def writer_file(self, store: type[WriterOwnedStore]) -> WriterOwnedInstallationFile:
        return WriterOwnedInstallationFile(store)

    def writer_files(self) -> tuple[WriterOwnedInstallationFile, ...]:
        WriterOwnedStore.load_declarations()
        return tuple(self.writer_file(store)
                     for store in WriterOwnedStore.members_with(WriterOwnedStore))

    def selected(self, paths: Iterable[Path]) -> tuple[InstallationFile, ...]:
        external = {file.path: file for file in self.writer_files()}
        selected = frozenset(paths)
        bus = selected - external.keys()
        for path in bus:
            if not path.is_relative_to(self.root):
                raise RelationViolationError(f"Installation refuses an undeclared file: {path}")
        files = (
            *(external[path] for path in selected & external.keys()),
            *(BusInstallationFile(self.root, resource, owner)
              for resource, owner in RootFile.owned_files(self.root, bus).items()),
        )
        for file in files:
            file.require_direct()
        names = [file.name for file in files]
        if len(set(names)) != len(names):
            raise RelationViolationError(f"Installation file names collide: {names}")
        return tuple(sorted(files, key=lambda file: file.name))

    def referenced(self, path: Path) -> tuple[InstallationFile, ...]:
        """Decode a carry's physical path or retained name at the file boundary."""
        if path.is_absolute():
            return self.selected((path,))
        return self.named((path,))

    def named(self, names: Iterable[Path]) -> tuple[InstallationFile, ...]:
        external = {file.name: file for file in self.writer_files()}
        selected = frozenset(names)
        for name in selected:
            if name.is_relative_to(WriterOwnedInstallationFile.directory) and name not in external:
                raise RelationViolationError(f"Unknown writer-owned installation file: {name}")
            if name.is_absolute() or ".." in name.parts:
                raise RelationViolationError(f"Installation refuses a retained name: {name}")
        return self.selected(tuple(
            external[name].path for name in selected & external.keys()
        ) + tuple(self.root / name for name in selected - external.keys()))


class CopiedInstallationFiles(InstallationFiles):
    def writer_file(self, store: type[WriterOwnedStore]) -> CopiedWriterOwnedFile:
        return CopiedWriterOwnedFile(store, self.root)
