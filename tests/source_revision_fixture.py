"""One real Git source/remote owner for citation tests; no source checkout is required."""
import ast
from collections.abc import Mapping
from pathlib import Path
import subprocess
from typing import ClassVar, Self
from types import MappingProxyType
from tempfile import mkdtemp

import tomli_w

from agent_comms.registry_document import RegisteredCommanderProject
from agent_comms.repo import Repo
from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.repository_name import RepositoryName
from agent_comms.todos import OrderRoots
from agent_comms.change_report import FileLine
from agent_comms.todos import FixScope, FixShape, PullRequestLink
from fixture_git import IDENTITY


class SourceRepository(Repo):
    main_source: ClassVar[str] = 'value = 1\nclass Parent:\n    pass\n'
    number: int = 7

    @property
    def remote_path(self) -> Path:
        return Path(self.git(self.checkout, 'remote', 'get-url', self.remote).strip())

    @property
    def main_commit(self) -> str:
        return self.git(self.checkout, 'rev-parse', 'refs/heads/main').strip()

    @property
    def branch_commit(self) -> str:
        return self.git(self.checkout, 'rev-parse', 'refs/heads/feature').strip()

    @property
    def project(self) -> RegisteredCommanderProject:
        return RegisteredCommanderProject(RecordedThreadIncarnation('lead', 1.0), (self,))

    @property
    def roots(self) -> OrderRoots:
        return OrderRoots(self.checkout, self.project)

    @property
    def link(self) -> PullRequestLink:
        return PullRequestLink(self.github, self.number)

    @classmethod
    def scope(cls) -> FixScope:
        parent = next(node for node in ast.parse(cls.main_source).body if isinstance(node, ast.ClassDef))
        return FixScope(FixShape(parents=(FileLine('owner.py', parent.lineno),), members=()))

    @staticmethod
    def _run(checkout: Path | str, *args: str, timeout: float = 60,
             input: bytes = b"") -> subprocess.CompletedProcess[bytes]:
        """Repo's one git subprocess, every command carrying the fixture identity."""
        return Repo._run(checkout, *IDENTITY, *args, timeout=timeout, input=input)

    @staticmethod
    def git_input(repository: Path, text: str, *arguments: str) -> str:
        return subprocess.run(
            ('git', *IDENTITY, '-C', str(repository), *arguments), input=text, text=True,
            capture_output=True, check=True, timeout=30,
        ).stdout.strip()

    @staticmethod
    def package_project(
        name: str, *, dependencies: tuple[str, ...] = (), python: str = ">=3.10",
        extras: dict[str, list[str]] | None = None, scripts: Mapping[str, str] = MappingProxyType({}),
        build_system: Mapping[str, str | tuple[str, ...]] = MappingProxyType({
            "requires": ("hatchling",), "build-backend": "hatchling.build",
        }),
    ) -> str:
        """Build the actual src tree, independently of distribution or module spelling."""
        return tomli_w.dumps({
            "build-system": dict(build_system),
            "project": {"name": name, "version": "0.0.1", "requires-python": python,
                        "dependencies": list(dependencies), "optional-dependencies": extras or {},
                        "scripts": dict(scripts)},
            "tool": {"hatch": {"build": {"targets": {"wheel": {
                "only-include": ["src"], "sources": ["src"],
            }}}}},
        })

    def write_files(self, files: Mapping[Path, str]) -> tuple[str, ...]:
        """Real source fixtures share file preparation, not a second source-selection mechanism."""
        for relative, text in files.items():
            destination = self.checkout / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(text)
        return tuple(str(path) for path in files)

    def commit_change(self, message: str, *files: str) -> str:
        """Commit the current checkout; its caller chooses what ref to publish."""
        self.git(self.checkout, 'add', '--', *files)
        self.git(self.checkout, 'commit', '-m', message)
        return self.git(self.checkout, 'rev-parse', 'HEAD').strip()

    def publish_change(self, message: str, *files: str) -> str:
        """Commit fixture files and publish this PR's descendant through real Git."""
        head = self.commit_change(message, *files)
        self.git(self.checkout, 'push', 'origin', f'HEAD:refs/pull/{self.number}/head')
        return head

    @classmethod
    def remote_for(cls, root: Path, repository: RepositoryName) -> Path:
        remote = root / f'{repository.to_wire()}.git'
        remote.parent.mkdir(parents=True, exist_ok=True)
        cls.git(root, 'init', '--bare', '--initial-branch=main', str(remote))
        return remote

    @classmethod
    def create(
        cls, root: Path, name: RepositoryName = RepositoryName.from_wire('example/source'), *,
        baseline_files: Mapping[Path, str] = MappingProxyType({}),
    ) -> Self:
        root.mkdir(parents=True, exist_ok=True)
        root = Path(mkdtemp(prefix='source-', dir=root))
        repository = root / 'repository'
        repository.mkdir(parents=True)
        remote = cls.remote_for(root, name)
        cls.git(repository, 'init', '--initial-branch=main')
        cls.git(repository, 'remote', 'add', 'origin', str(remote))
        created = cls.of(repository)
        assert isinstance(created, cls)
        created.commit_change('Main declaration', *created.write_files({
            Path('owner.py'): cls.main_source, **baseline_files,
        }))
        cls.git(repository, 'switch', '-c', 'feature')
        created.commit_change('PR declaration', *created.write_files({
            Path('owner.py'): (repository / 'owner.py').read_text() + 'class Member(Parent):\n    pass\n',
            Path('branch.py'): '@decorated\nclass BranchOnly:\n    pass\n',
        }))
        cls.git(repository, 'push', 'origin', 'main', f'feature:refs/pull/{created.number}/head')
        cls.git(repository, 'remote', 'set-head', 'origin', '--auto')
        cls.git(repository, 'switch', 'main')
        return created
