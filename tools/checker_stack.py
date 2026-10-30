"""Build the one checker environment from materialized CI sources, never resolve sources here."""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
import tomllib
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

import tomli_w
from module_layers import MaterializedClients
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from agent_comms.head_review import BaseSide, HeadSide, IntegrationInput, Side
from agent_comms.installer.commands import InstallCommands
from agent_comms.installer.declared_uv import DeclaredUv
from agent_comms.installer.runtime_resolution import (
    CorePackage,
    PrivateUvProject,
    ResolvedLock,
    RuntimeResolution,
    StackSources,
    VersionedPackage,
)
from agent_comms.installer.upgrade import Stack
from agent_comms.integration import RunnerEnvironment
from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.repo import Repo


@dataclass(frozen=True, kw_only=True)
class CheckerStack(Stack, StackSources):
    """Stack's command/probe/runtime lifetime, with a private unchanged-settings CI project.

    This consumes IntegrationInput. It cannot choose a new source, read the live bus,
    post a verdict, or replace a production project/lock/interpreter.
    """

    source_directory: Path
    inputs: IntegrationInput
    uv_program: Path

    @property
    def uv(self) -> Path:
        return self.uv_program

    @cached_property
    def core(self) -> Path:
        return HeadSide.require_materialized(
            self.source_directory, self.inputs.source(CorePackage.repository)
        )

    @cached_property
    def local_names(self) -> frozenset[str]:
        source = self.inputs.source(CorePackage.repository)
        return self.local_packages(
            *(
                tomllib.loads(
                    (
                        side.require_materialized(self.source_directory, source)
                        / "stack/pyproject.toml"
                    ).read_text()
                )
                for side in Side.sides()
            )
        )

    def require_uv(self) -> None:
        declared = DeclaredUv.of(self.core / "pyproject.toml", self.uv)
        if not declared.accepts(self.commands.run(self.uv, "--version")):
            raise ValueError("The bootstrap checker must use Core's declared uv")

    @cached_property
    def clients(self) -> MaterializedClients:
        return MaterializedClients(self.source_directory, self.inputs, self.uv, self.python)

    def requirements(self, side: type[Side]) -> tuple[Requirement, ...]:
        core = side.require_materialized(
            self.source_directory, self.inputs.source(CorePackage.repository)
        )
        # Identity validation and runtime selection borrow the SAME backend metadata objects.
        projects = self.clients.projects(side)
        return self.active_requirements(
            tomllib.loads((core / "stack/pyproject.toml").read_text()),
            tuple(projects.values()),
            self.clients.environment,
        )

    def require_runtime_compatibility(self) -> None:
        # Local package presence differs legitimately; only the real external contract agrees.
        before, after = (
            frozenset(
                str(requirement)
                for requirement in self.requirements(side)
                if canonicalize_name(requirement.name) not in self.local_names
            )
            for side in (BaseSide, HeadSide)
        )
        if before != after:
            raise ValueError(
                "External runtime dependency migration requires an explicit compatible environment"
            )

    @cached_property
    def bootstrap_lock(self) -> ResolvedLock:
        return ResolvedLock.read(self.core / "uv.lock")

    @cached_property
    def runtime_locks(self) -> dict[type[Side], ResolvedLock]:
        source = self.inputs.source(CorePackage.repository)
        return {
            side: ResolvedLock.read(
                side.require_materialized(self.source_directory, source) / "stack/uv.lock"
            )
            for side in Side.sides()
        }

    @cached_property
    def shared_names(self) -> frozenset[str]:
        runtime_names = {
            name for lock in self.runtime_locks.values() for name in lock.external(self.local_names)
        }
        return frozenset(runtime_names & self.bootstrap_lock.external(self.local_names).keys())

    @cached_property
    def retained_packages(self) -> dict[str, VersionedPackage]:
        """Derived disjoint lock evidence, never another editable selection ledger."""
        excluded = self.local_names | self.shared_names
        retained = self.bootstrap_lock.external(excluded)
        for lock in self.runtime_locks.values():
            for name, package in lock.external(excluded).items():
                if name in retained:
                    retained[name].require_retained(package)
                retained[name] = package
        return retained

    def private_requirements(self) -> tuple[Requirement, ...]:
        return self.active_requirements(
            tomllib.loads((self.checkout / "stack/pyproject.toml").read_text()),
            tuple(self.clients.projects(HeadSide).values()),
            self.clients.environment,
        )

    def write_project(self) -> None:
        """Declare both complete root graphs and retained identities before the one solve."""
        self.require_runtime_compatibility()
        retained = (
            self.retained_packages
        )  # Conflicting runtime-only locks refuse before prefix mutation.
        original = self.core / "stack"
        project = self.checkout / "stack"
        project.mkdir(parents=True, exist_ok=False)
        document = PrivateUvProject.read(original / "pyproject.toml")
        projects = self.clients.projects(HeadSide)
        document.enrich(projects[CorePackage.repository])
        materialized = {
            identity: (
                HeadSide.require_materialized(self.source_directory, self.inputs.source(identity)),
                metadata,
            )
            for identity, metadata in projects.items()
        }
        document.materialize(original, self.core, materialized)
        for package in retained.values():
            package.constrain(document.tool.uv)
        (project / "pyproject.toml").write_text(tomli_w.dumps(document.document()))
        shutil.copyfile(original / "uv.lock", self.lock)

    def resolve(self) -> None:
        # No --upgrade: unrelated release and revision pins survive the local-path projection.
        self.commands.run(
            self.uv, "lock", "--project", self.checkout / "stack", "--python", self.python
        )

    def require_resolution(self, resolution: RuntimeResolution) -> None:
        locked = ResolvedLock.read(self.lock)
        external = locked.external(self.local_names)
        selected = {
            name: record.version
            for name, record in resolution.packages.items()
            if name not in self.local_names
        }
        wanted = locked.pins(selected, resolution.core_commit)
        actual = {name: resolution.packages[name].pin() for name in selected}
        if actual != wanted:
            raise ValueError("The one actual checker environment differs from its private lock")
        for name, package in self.retained_packages.items():
            # A universal lock can include an unselected optional dependency. Constraints
            # preserve its identity without inventing a request to install that extra.
            if name in external:
                package.require_retained(external[name])
        for requirements in (
            *(self.requirements(side) for side in Side.sides()),
            self.private_requirements(),
        ):
            for requirement in requirements:
                record = resolution.packages[canonicalize_name(requirement.name)]
                if not requirement.specifier.contains(record.version, prereleases=True):
                    raise ValueError(f"The common environment does not satisfy {requirement}")
        for name in sorted(self.shared_names & selected.keys()):
            preferences = [
                package.version
                for lock in (*self.runtime_locks.values(), self.bootstrap_lock)
                for key, package in lock.external(self.local_names).items()
                if key == name
            ]
            print(
                f"CHECKER SHARED {name}: original={preferences} solved={selected[name]}", flush=True
            )

    def prepare(self) -> RuntimeResolution:
        prefix = self.runtime.prefix.resolve()
        runner = RunnerEnvironment.read().root.resolve()
        for path in (self.source_directory.resolve(), prefix):
            if not path.is_relative_to(runner):
                raise ValueError(
                    "The common checker requires its isolated runner bootstrap prefix"
                    " and input directory"
                )
        if not self.checkout.resolve().is_relative_to(self.source_directory.resolve()):
            raise ValueError(
                "The private checker project must be under its captured source directory"
            )
        if not (prefix / "pyvenv.cfg").is_file():
            raise ValueError(
                "The common checker requires its isolated runner bootstrap prefix"
                " and input directory"
            )
        self.require_uv()
        self.write_project()
        self.resolve()
        # Both graphs belong to this solve; retaining stray files cannot substitute for roots.
        self.commands.run(
            self.uv,
            "sync",
            "--project",
            self.checkout / "stack",
            "--locked",
            "--no-editable",
            "--python",
            self.python,
            env={**os.environ, "UV_PROJECT_ENVIRONMENT": str(self.runtime.prefix)},
        )
        self.commands.run(self.uv, "pip", "check", "--python", self.python)
        commit = Repo.git(self.core, "rev-parse", "HEAD").strip()
        resolution = RuntimeResolution.capture(self, commit)
        self.require_resolution(resolution)
        resolution.save(self.runtime.prefix)
        digest = hashlib.sha256(self.lock.read_bytes()).hexdigest()
        bootstrap_digest = hashlib.sha256((self.core / "uv.lock").read_bytes()).hexdigest()
        print(
            f"CHECKER INTERPRETER {self.python}\nCHECKER LOCK {digest}\n"
            f"CHECKER BOOTSTRAP LOCK {bootstrap_digest}\n{resolution.model_dump_json(indent=2)}",
            flush=True,
        )
        return resolution


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ci-inputs", type=Path, required=True)
    args = parser.parse_args()
    directory = args.ci_inputs.resolve()
    executable = StackSources.frontend()
    inputs = IntegrationInput.at(directory)
    with InstallCommands() as commands:
        CheckerStack(
            checkout=directory / "checker",
            commands=commands,
            home=directory,
            runtime=LaunchRuntime(Path(sys.executable)),
            source_directory=directory,
            inputs=inputs,
            uv_program=executable,
        ).prepare()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
