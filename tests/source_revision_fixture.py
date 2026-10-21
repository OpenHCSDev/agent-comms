"""One real Git source/remote owner for citation tests; no source checkout is required."""
import ast
from functools import cache
from pathlib import Path
import subprocess
from typing import ClassVar

from agent_comms.repo import Repo
from agent_comms.repository_name import RepositoryName
from agent_comms.change_report import FileLine
from agent_comms.todos import FixScope, FixShape, PullRequestLink


class SourceRepository(Repo):
    main_source: ClassVar[str] = 'value = 1\nclass Parent:\n    pass\n'
    remote: Path
    main_commit: str
    branch_commit: str
    number: int = 7

    @property
    def link(self) -> PullRequestLink:
        return PullRequestLink(self.github, self.number)

    @classmethod
    def scope(cls) -> FixScope:
        parent = next(node for node in ast.parse(cls.main_source).body if isinstance(node, ast.ClassDef))
        return FixScope(FixShape(parents=(FileLine('owner.py', parent.lineno),), members=()))

    @staticmethod
    def git_input(repository: Path, text: str, *arguments: str) -> str:
        return subprocess.run(
            ('git', '-C', str(repository), *arguments), input=text, text=True,
            capture_output=True, check=True, timeout=30,
        ).stdout.strip()

    @classmethod
    @cache
    def create(cls, root: Path):
        repository, remote = root / 'repository', root / 'remote'
        repository.mkdir(parents=True)
        remote.mkdir()
        cls.git(remote, 'init', '--bare', '--initial-branch=main')
        cls.git(repository, 'init', '--initial-branch=main')
        cls.git(repository, 'config', 'user.name', 'Citation fixture')
        cls.git(repository, 'config', 'user.email', 'citation@example.invalid')
        cls.git(repository, 'config', 'commit.gpgsign', 'false')
        cls.git(repository, 'remote', 'add', 'origin', str(remote))
        main_blob = cls.git_input(repository, cls.main_source,
                                  'hash-object', '-w', '--stdin')
        main_tree = cls.git_input(repository, f'100644 blob {main_blob}\towner.py\n', 'mktree')
        main = cls.git(repository, 'commit-tree', main_tree, '-m', 'Main declaration').strip()
        cls.git(repository, 'update-ref', 'refs/heads/main', main)
        branch_blob = cls.git_input(repository,
            'value = 1\nclass Parent:\n    pass\nclass Member(Parent):\n    pass\n',
            'hash-object', '-w', '--stdin')
        new_blob = cls.git_input(repository, '@decorated\nclass BranchOnly:\n    pass\n',
                                'hash-object', '-w', '--stdin')
        tree = cls.git_input(repository,
            f'100644 blob {new_blob}\tbranch.py\n100644 blob {branch_blob}\towner.py\n', 'mktree')
        branch = cls.git(repository, 'commit-tree', tree, '-p', main, '-m', 'PR declaration').strip()
        cls.git(repository, 'update-ref', 'refs/heads/feature', branch)
        cls.git(repository, 'push', 'origin', 'main', 'feature:refs/pull/7/head')
        return cls(checkout=repository, github=RepositoryName.from_wire('example/source'),
                   remote=remote, main_commit=main, branch_commit=branch)
