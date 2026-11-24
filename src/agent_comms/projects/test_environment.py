"""Synchronize a repository's shared tests from its committed requirements."""

import os
import tomllib
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import tempfile

from agent_comms.execution.launch_runtime import LaunchRuntime
from agent_comms.execution.runtime_processes import RuntimeProcesses
from agent_comms.execution.test_lease import LeaseStart, TestLease
from agent_comms.installer.commands import InstallCommands
from agent_comms.nominal.field_codec import Id128
from agent_comms.projects.checkouts import Checkout
from agent_comms.projects.repo import Repo
from agent_comms.projects.repository_environment import RepositoryEnvironment
from agent_comms.storage.store_files import AtomicFile


class RetainedEnvironmentId(Id128):
    """One retained test-environment snapshot."""


@dataclass(frozen=True)
class DeclaredTestEnvironment:
    checkout: Checkout
    requirements: Path

    @classmethod
    def selected(cls, checkout: Checkout, declaration: Path) -> DeclaredTestEnvironment:
        """Decode the external declaration format once, before synchronization."""
        if declaration.name == "pyproject.toml":
            return LockedProjectTestEnvironment(checkout, declaration)
        return cls(checkout, declaration)

    @property
    def environment(self) -> RepositoryEnvironment:
        return RepositoryEnvironment.of(self.checkout.path)

    def committed(self, path: Path) -> tuple[Path, str, str]:
        """The exact committed input, never a guessed file in another worktree."""
        checkout = self.checkout.resolved(strict=True).path
        declaration = (checkout / path).resolve(strict=True)
        if not declaration.is_relative_to(checkout):
            raise ValueError(f"Declaration {declaration} escapes {checkout}")
        relative = declaration.relative_to(checkout).as_posix()
        revision = Repo.git(checkout, "rev-parse", "HEAD").strip()
        committed = Repo.git(checkout, "show", f"{revision}:{relative}")
        content = declaration.read_bytes()
        if content != committed.encode("utf-8"):
            raise ValueError(f"Commit {relative} before syncing the shared environment")
        return declaration, revision, hashlib.sha256(content).hexdigest()

    def source(self) -> tuple[Path, str, str]:
        return self.committed(self.requirements)

    def require_unused(self) -> None:
        prefix = self.environment.prefix
        if prefix.resolve() != prefix:
            raise ValueError(f"Test environment {prefix} must be a physical prefix, not a symlink")
        if LaunchRuntime.running().prefix == prefix:
            raise ValueError(f"Run this operation outside {prefix}; it would replace its own packages")
        observed = RuntimeProcesses.under(prefix)
        held = tuple(line for outcome in observed.outcomes
                     if any(path.is_relative_to(prefix) for path in outcome.held_paths())
                     for line in (outcome.line(),))
        if held or observed.unread():
            raise ValueError(f"Test environment {prefix} is in use or unreadable: "
                             + "; ".join((*held, *observed.unread())))

    def resolve(self, commands: InstallCommands, uv: Path, python: Path, directory: Path) -> Path:
        lock = directory / "requirements.lock"
        commands.run(uv, "pip", "compile", self.checkout.path / self.requirements,
                     "--python", python, "--generate-hashes", "--output-file", lock,
                     cwd=self.checkout.path)
        return lock

    def install(self, commands: InstallCommands, uv: Path, python: Path, lock: Path) -> None:
        runtime = LaunchRuntime.at_prefix(self.environment.prefix)
        commands.run(uv, "venv", "--python", python, "--allow-existing", runtime.prefix,
                     cwd=self.checkout.path)
        commands.run(uv, "pip", "sync", "--python", runtime.interpreter,
                     "--require-hashes", lock, cwd=self.checkout.path)

    def retain_installed(self, commands: InstallCommands, uv: Path) -> str:
        """Keep original installed sources and the last receipt before any replacement."""
        environment = self.environment
        if not environment.prefix.exists():
            return ""
        runtime = LaunchRuntime.at_prefix(environment.prefix)
        sources = commands.run(uv, "pip", "freeze", "--python", runtime.interpreter,
                               cwd=self.checkout.path)
        packages = commands.run(uv, "pip", "list", "--python", runtime.interpreter,
                                "--format", "json", cwd=self.checkout.path)
        receipt = (environment.declaration_record.read_text()
                   if environment.declaration_record.exists() else "")
        retained = environment.history / f"{RetainedEnvironmentId.mint()}.json"
        AtomicFile(retained).write_text(json.dumps({"prefix": str(runtime.prefix),
            "sources": sources, "packages": json.loads(packages), "receipt": receipt}, indent=2) + "\n")
        return str(retained)

    def sync(self, commands: InstallCommands, uv: Path, python: Path,
             *, start: LeaseStart = TestLease.default_start) -> dict[str, object]:
        declaration, revision, digest = self.source()
        environment = self.environment
        with TestLease.current().held(start):
            self.require_unused()
            with tempfile.TemporaryDirectory(dir=environment.common, prefix="test-environment-") as temporary:
                lock = self.resolve(commands, uv, python, Path(temporary))
                if self.source() != (declaration, revision, digest):
                    raise ValueError("The declaration changed during resolution; sync did not run")
                previous = self.retain_installed(commands, uv)
                self.require_unused()
                record = {"checkout": str(self.checkout.resolved().path), "commit": revision,
                          "requirements": str(declaration), "sha256": digest,
                          "prefix": str(environment.prefix), "python": str(python),
                          "lock": lock.read_text(), "previous": previous, "completed": False}
                publication = AtomicFile(environment.declaration_record)
                publication.write_text(json.dumps(record, indent=2) + "\n")
                self.install(commands, uv, python, lock)
                commands.run(uv, "pip", "check", "--python",
                             LaunchRuntime.at_prefix(environment.prefix).interpreter,
                             cwd=self.checkout.path)
                if self.source() != (declaration, revision, digest):
                    raise ValueError("The declaration changed during installation; inspect the incomplete receipt")
                record["completed"] = True
                publication.write_text(json.dumps(record, indent=2) + "\n")
                return record


@dataclass(frozen=True)
class LockedProjectTestEnvironment(DeclaredTestEnvironment):
    """The committed project and uv lock own the project install and all test groups."""

    def source(self) -> tuple[Path, str, str]:
        declaration, revision, digest = super().source()
        _, lock_revision, lock_digest = self.committed(self.requirements.with_name("uv.lock"))
        if lock_revision != revision or Repo.git(
                self.checkout.path, "status", "--porcelain", "--untracked-files=no").strip():
            raise ValueError("Commit the project source and lock before syncing its shared environment")
        return declaration, revision, hashlib.sha256((digest + lock_digest).encode()).hexdigest()

    def resolve(self, commands: InstallCommands, uv: Path, python: Path, directory: Path) -> Path:
        declaration, _, _ = self.source()
        commands.run(uv, "lock", "--check", "--project", declaration.parent,
                     "--python", python, cwd=self.checkout.path)
        lock = directory / "uv.lock"
        lock.write_bytes(declaration.with_name("uv.lock").read_bytes())
        return lock

    def install(self, commands: InstallCommands, uv: Path, python: Path, lock: Path) -> None:
        declaration, _, _ = self.source()
        project = tomllib.loads(declaration.read_text())["project"]["name"]
        # Rebuild self even when an old editable install's version still agrees.
        commands.run(uv, "sync", "--project", declaration.parent, "--locked",
                     "--all-extras", "--all-groups", "--reinstall-package", project,
                     "--python", python, cwd=self.checkout.path,
                     env={**os.environ, "UV_PROJECT_ENVIRONMENT": str(self.environment.prefix)})
