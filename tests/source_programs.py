"""Real console targets from this checkout, using the existing shared Python."""

import sys
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from importlib.metadata import EntryPoint
from pathlib import Path
from typing import Self


@dataclass(frozen=True)
class SourcePrograms:
    prefix: Path
    entries: tuple[EntryPoint, ...]

    def script(self, function: Callable[..., object]) -> Path:
        target = f'{function.__module__}:{function.__name__}'
        (entry,) = (entry for entry in self.entries if entry.value == target)
        return self.prefix / 'bin' / entry.name

    @classmethod
    def create(cls, directory: Path) -> Self:
        prefix = directory / 'source-programs'
        binary = prefix / 'bin'
        binary.mkdir(parents=True)
        (binary / 'python').symlink_to(sys.executable)
        project = Path(__file__).resolve().parents[1]
        source = str(project / 'src')
        scripts = tomllib.loads((project / 'pyproject.toml').read_text())['project']['scripts']
        entries = tuple(EntryPoint(name=name, value=value, group='console_scripts')
                        for name, value in scripts.items())
        for entry in entries:
            program = binary / entry.name
            program.write_text(
                f'#!{sys.executable}\nimport sys\n'
                f'sys.path.insert(0, {source!r})\n'
                'from importlib.metadata import EntryPoint\n'
                f'sys.exit(EntryPoint(name={entry.name!r}, value={entry.value!r}, '
                f'group={entry.group!r}).load()())\n'
            )
            program.chmod(0o700)
        return cls(prefix, entries)
