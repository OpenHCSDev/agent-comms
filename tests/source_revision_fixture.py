"""One real Git source/remote owner for citation tests; no source checkout is required."""
import ast
from pathlib import Path
import subprocess
from typing import ClassVar
from tempfile import mkdtemp

from agent_comms.repo import Repo
from agent_comms.repository_name import RepositoryName
from agent_comms.change_report import FileLine
from agent_comms.todos import FixScope, FixShape, PullRequestLink
from fixture_git import IDENTITY


class SourceRepository(Repo):
    main_source: ClassVar[str] = 'value = 1\nclass Parent:\n    pass\n'
    number: int = 7

    @property
    def remote(self) -> Path:
        return Path(self.git(self.checkout, 'remote', 'get-url', 'origin').strip())

    @property
    def main_commit(self) -> str:
        return self.git(self.checkout, 'rev-parse', 'refs/heads/main').strip()

    @property
    def branch_commit(self) -> str:
        return self.git(self.checkout, 'rev-parse', 'refs/heads/feature').strip()

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

    @classmethod
    def remote_for(cls, root: Path, repository: RepositoryName) -> Path:
        remote = root / f'{repository.to_wire()}.git'
        remote.parent.mkdir(parents=True, exist_ok=True)
        cls.git(root, 'init', '--bare', '--initial-branch=main', str(remote))
        return remote

    @classmethod
    def create(cls, root: Path, name: RepositoryName = RepositoryName.from_wire('example/source')):
        root.mkdir(parents=True, exist_ok=True)
        root = Path(mkdtemp(prefix='source-', dir=root))
        repository = root / 'repository'
        repository.mkdir(parents=True)
        remote = cls.remote_for(root, name)
        cls.git(repository, 'init', '--initial-branch=main')
        cls.git(repository, 'remote', 'add', 'origin', str(remote))
        main_blob = cls.git_input(repository, cls.main_source,
                                  'hash-object', '-w', '--stdin')
        main_tree = cls.git_input(repository, f'100644 blob {main_blob}\towner.py\n', 'mktree')
        main = cls.git(repository, 'commit-tree', main_tree, '-m', 'Main declaration').strip()
        cls.git(repository, 'update-ref', 'refs/heads/main', main)
        branch_blob = cls.git_input(repository,
            cls.main_source + 'class Member(Parent):\n    pass\n',
            'hash-object', '-w', '--stdin')
        new_blob = cls.git_input(repository, '@decorated\nclass BranchOnly:\n    pass\n',
                                'hash-object', '-w', '--stdin')
        tree = cls.git_input(repository,
            f'100644 blob {new_blob}\tbranch.py\n100644 blob {branch_blob}\towner.py\n', 'mktree')
        branch = cls.git(repository, 'commit-tree', tree, '-p', main, '-m', 'PR declaration').strip()
        cls.git(repository, 'update-ref', 'refs/heads/feature', branch)
        cls.git(repository, 'push', 'origin', 'main', 'feature:refs/pull/7/head')
        cls.git(repository, 'reset', '--hard', 'main')
        return cls.of(repository)
