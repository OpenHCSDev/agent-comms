"""Copy committed stores through their existing SQLite and staging owners."""

from __future__ import annotations

import shutil
import sqlite3
import stat
from collections.abc import Collection
from contextlib import closing
from pathlib import Path

from .coordination_database import CoordinationStore
from .locked_store import LockedStore


class CommittedStoreCopy:
    @staticmethod
    def copy_file(source: str | Path, target: str | Path) -> Path:
        """Copy one store through its SQLite snapshot, never through live WAL bytes."""
        source, target = Path(source), Path(target)
        if CoordinationStore.is_database(source):
            with (
                closing(CoordinationStore.read_snapshot(source)) as reader,
                closing(sqlite3.connect(target)) as writer,
            ):
                reader.backup(writer)
            shutil.copystat(source, target)
        else:
            shutil.copy2(source, target)
        return target

    @classmethod
    def copy_tree(cls, source: Path, target: Path, *, excluded: Collection[Path] = ()) -> None:
        """Copy committed files; their owners declare transient names before any stat."""

        def transient(directory: str, names: list[str]) -> set[str]:
            omitted: set[str] = set()
            for name in sorted(names):
                path = Path(directory) / name
                if name in omitted or path in excluded or LockedStore.is_staging(path):
                    omitted.add(name)
                    continue
                if stat.S_ISSOCK(path.lstat().st_mode):
                    omitted.add(name)
                elif (
                    path.is_file() and not path.is_symlink() and CoordinationStore.is_database(path)
                ):
                    omitted.update(
                        companion.name for companion in CoordinationStore.files(path)[1:]
                    )
            return omitted

        shutil.copytree(
            source, target, symlinks=True, ignore=transient, copy_function=cls.copy_file
        )
