"""Synchronize a repository's shared tests from its committed requirements."""

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import tempfile

from agent_comms.execution.launch_runtime import LaunchRuntime
from agent_comms.execution.runtime_processes import RuntimeProcesses
from agent_comms.execution.test_lease import LeaseStart, TestLease
from agent_comms.installer.commands import InstallCommands
from agent_comms.projects.checkouts import Checkout
from agent_comms.projects.repo import Repo
from agent_comms.projects.repository_environment import RepositoryEnvironment
from agent_comms.storage.store_files import AtomicFile


@dataclass(frozen=True)
class DeclaredTestEnvironment:
    checkout: Checkout
    requirements: Path

    @property
    def environment(self) -> RepositoryEnvironment:
        return RepositoryEnvironment.of(self.checkout.path)

    def source(self) -> tuple[Path, str, str]:
        """The exact committed declaration, never a guessed file in another worktree."""
        checkout = self.checkout.resolved(strict=True).path
        declaration = (checkout / self.requirements).resolve(strict=True)
        if not declaration.is_relative_to(checkout):
            raise ValueError(f"Requirements {declaration} escape {checkout}")
        relative = declaration.relative_to(checkout).as_posix()
        revision = Repo.git(checkout, "rev-parse", "HEAD").strip()
        committed = Repo.git(checkout, "show", f"{revision}:{relative}")
        if declaration.read_bytes() != committed.encode("utf-8"):
            raise ValueError(f"Commit {relative} before syncing the shared environment")
        return declaration, revision, hashlib.sha256(declaration.read_bytes()).hexdigest()

    def require_unused(self) -> None:
        prefix = self.environment.prefix
        if LaunchRuntime.running().prefix == prefix:
            raise ValueError(f"Run this operation outside {prefix}; it would replace its own packages")
        observed = RuntimeProcesses.under(prefix)
        held = tuple(line for outcome in observed.outcomes
                     if any(path.is_relative_to(prefix) for path in outcome.held_paths())
                     for line in (outcome.line(),))
        if held or observed.unread():
            raise ValueError(f"Test environment {prefix} is in use or unreadable: "
                             + "; ".join((*held, *observed.unread())))

    def sync(self, commands: InstallCommands, uv: Path, python: Path,
             *, start: LeaseStart = TestLease.default_start) -> dict[str, object]:
        declaration, revision, digest = self.source()
        environment = self.environment
        # The same kernel-held lease that protects test runs serializes replacement.
        with TestLease.current().held(start):
            self.require_unused()
            if environment.prefix.exists() and not environment.declaration_record.exists():
                raise ValueError(f"{environment.prefix} has no requirements sync record; "
                                 "retain and inspect its installed sources before replacing it")
            with tempfile.TemporaryDirectory(dir=environment.common, prefix="test-environment-") as temporary:
                lock = Path(temporary) / "requirements.lock"
                commands.run(uv, "pip", "compile", declaration, "--python", python,
                             "--generate-hashes", "--output-file", lock, cwd=self.checkout.path)
                # Compilation must not quietly select an edited or switched declaration.
                if self.source() != (declaration, revision, digest):
                    raise ValueError("The requirements declaration changed during resolution; sync did not run")
                self.require_unused()
                record = {"checkout": str(self.checkout.resolved().path), "commit": revision,
                          "requirements": str(declaration), "sha256": digest,
                          "prefix": str(environment.prefix), "python": str(python),
                          "lock": lock.read_text(), "completed": False}
                publication = AtomicFile(environment.declaration_record)
                publication.write_text(json.dumps(record, indent=2) + "\n")
                runtime = LaunchRuntime.at_prefix(environment.prefix)
                commands.run(uv, "venv", "--python", python, "--allow-existing", runtime.prefix,
                             cwd=self.checkout.path)
                commands.run(uv, "pip", "sync", "--python", runtime.interpreter,
                             "--require-hashes", lock, cwd=self.checkout.path)
                commands.run(uv, "pip", "check", "--python", runtime.interpreter,
                             cwd=self.checkout.path)
                record["completed"] = True
                publication.write_text(json.dumps(record, indent=2) + "\n")
                return record
