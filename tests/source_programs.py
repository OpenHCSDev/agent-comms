"""Real console targets from this checkout, using the existing shared Python."""

import shutil
import sys
import sysconfig
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from importlib.metadata import EntryPoint
from pathlib import Path
from typing import Self

from agent_comms.launch_runtime import LaunchRuntime


@dataclass(frozen=True)
class SourcePrograms:
    prefix: Path
    entries: tuple[EntryPoint, ...]
    packages: tuple[Path, ...]

    def bind(self, directory: Path) -> None:
        """Let real Python workers import this checkout from their own cwd."""
        for package in self.packages:
            (directory / package.name).symlink_to(package, target_is_directory=True)

    @property
    def runtime(self) -> LaunchRuntime:
        """The runtime these programs report: the interpreter this prefix declares."""
        return LaunchRuntime(self.prefix / 'bin' / 'python')

    def script(self, function: Callable[..., object]) -> Path:
        target = f'{function.__module__}:{function.__name__}'
        (entry,) = (entry for entry in self.entries if entry.value == target)
        return self.prefix / 'bin' / entry.name

    @classmethod
    def create(cls, directory: Path) -> Self:
        prefix = directory / 'source-programs'
        binary = prefix / 'bin'
        binary.mkdir(parents=True)
        # A runtime prefix like an installed one: its programs report this prefix as their
        # runtime, and borrow only the shared Python's dependencies.
        interpreter = binary / 'python'
        interpreter.symlink_to(sys.executable)
        shutil.copy(Path(sys.prefix) / 'pyvenv.cfg', prefix / 'pyvenv.cfg')
        shared = sysconfig.get_path('purelib')
        site = prefix / Path(shared).relative_to(sys.prefix)
        site.mkdir(parents=True)
        (site / 'shared-python.pth').write_text(f'import site; site.addsitedir({shared!r})\n')
        project = Path(__file__).resolve().parents[1]
        manifest = tomllib.loads((project / 'pyproject.toml').read_text())
        packages = tuple(project / path for path in manifest['tool']['hatch']['build']['targets']['wheel']['packages'])
        sources = tuple(dict.fromkeys(str(package.parent) for package in packages))
        scripts = manifest['project']['scripts']
        entries = tuple(EntryPoint(name=name, value=value, group='console_scripts')
                        for name, value in scripts.items())
        for entry in entries:
            program = binary / entry.name
            program.write_text(
                f'#!{interpreter}\nimport sys\n'
                f'sys.path[:0] = {sources!r}\n'
                'from importlib.metadata import EntryPoint\n'
                f'sys.exit(EntryPoint(name={entry.name!r}, value={entry.value!r}, '
                f'group={entry.group!r}).load()())\n'
            )
            program.chmod(0o700)
        return cls(prefix, entries, packages)
