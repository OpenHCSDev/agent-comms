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

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
import tomli_w

from agent_comms.field_codec import FieldCodec
from agent_comms.head_review import BaseSide, HeadSide, IntegrationInput, Side
from agent_comms.installer.commands import InstallCommands
from agent_comms.installer.declared_uv import DeclaredUv
from agent_comms.installer.runtime_resolution import CorePackage, ResolvedLock, RuntimeResolution, StackSources
from agent_comms.installer.upgrade import Stack
from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.repo import Repo
from module_layers import MaterializedClients


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
        return HeadSide.require_materialized(self.source_directory, self.inputs.source(CorePackage.repository))

    @cached_property
    def local_names(self) -> frozenset[str]:
        source = self.inputs.source(CorePackage.repository)
        return frozenset({CorePackage.distribution, *(name for side in Side.sides()
            for name in self.sources(side.require_materialized(self.source_directory, source) / "stack"))})

    def require_uv(self) -> None:
        declared = DeclaredUv.of(self.core / "pyproject.toml", self.uv)
        if not declared.accepts(self.commands.run(self.uv, "--version")):
            raise ValueError("The bootstrap checker must use Core's declared uv")

    @cached_property
    def clients(self) -> MaterializedClients:
        return MaterializedClients(self.source_directory, self.inputs, self.uv, self.python, self.commands)

    def requirements(self, side: type[Side]) -> tuple[Requirement, ...]:
        core = side.require_materialized(self.source_directory, self.inputs.source(CorePackage.repository))
        # Identity validation and runtime selection borrow the SAME backend metadata objects.
        projects = self.clients.projects(side)
        return self.active_requirements(tomllib.loads((core / "stack/pyproject.toml").read_text()),
            tuple(projects.values()), self.clients.environment)

    def require_runtime_compatibility(self) -> None:
        # Local package presence differs legitimately; only the real external contract agrees.
        before, after = (frozenset(str(requirement) for requirement in self.requirements(side)
                                  if canonicalize_name(requirement.name) not in self.local_names)
                         for side in (BaseSide, HeadSide))
        if before != after:
            raise ValueError("External runtime dependency migration requires an explicit compatible environment")

    def write_project(self) -> None:
        """Preserve the full candidate document and pins; replace only its source entries."""
        self.require_runtime_compatibility()
        original = self.core / "stack"
        project = self.checkout / "stack"
        project.mkdir(parents=True, exist_ok=False)
        document: dict[str, object] = tomllib.loads((original / "pyproject.toml").read_text())
        match document:
            case {"tool": {"uv": {"sources": dict() as sources}}}:
                for raw_name, value in tuple(sources.items()):
                    name = canonicalize_name(FieldCodec.decode(str, raw_name))
                    match value:
                        case {"git": str(url), "branch": str(), **options}:
                            source = self.inputs.source(Repo.repository_url(url))
                            checkout = HeadSide.require_materialized(self.source_directory, source)
                            self.require_package(self.clients.projects(HeadSide)[source.repository], name)
                            sources[raw_name] = {**options, "path": str(checkout), "editable": True}
                        case {"path": str(relative), **options}:
                            checkout = (original / relative).resolve()
                            if checkout != self.core or name != CorePackage.distribution:
                                raise ValueError(f"Uncaptured local source {name}: {checkout}")
                            sources[raw_name] = {**options, "path": str(checkout)}
                        case _:
                            # Pinned/index/archive sources remain exactly as declared.
                            pass
            case _:
                raise ValueError("The candidate stack requires its actual source declarations")
        (project / "pyproject.toml").write_text(tomli_w.dumps(document))
        shutil.copyfile(original / "uv.lock", self.lock)

    def resolve(self) -> None:
        # No --upgrade: unrelated release and revision pins survive the local-path projection.
        self.commands.run(self.uv, "lock", "--project", self.checkout / "stack", "--python", self.python)

    def require_resolution(self, resolution: RuntimeResolution, original: ResolvedLock) -> None:
        locked = ResolvedLock.read(self.lock)
        names = {entry.name for entry in locked.package} - self.local_names
        selected = {name: record.version for name, record in resolution.packages.items() if name in names}
        wanted = locked.pins(selected, resolution.core_commit)
        actual = {name: resolution.packages[name].pin() for name in selected}
        if actual != wanted:
            raise ValueError("The one actual checker environment differs from its private lock")
        retained = {entry.name for entry in original.package} & selected.keys()
        versions = {name: selected[name] for name in retained}
        if original.pins(versions, resolution.core_commit) != locked.pins(versions, resolution.core_commit):
            raise ValueError("Resolving local sources changed a retained external release/revision pin")
        baseline = ResolvedLock.read(BaseSide.checkout(self.source_directory, CorePackage.repository) / "stack/uv.lock")
        shared = {entry.name for entry in baseline.package} & selected.keys()
        baseline_versions = {name: selected[name] for name in shared}
        if baseline.pins(baseline_versions, resolution.core_commit) != locked.pins(baseline_versions, resolution.core_commit):
            raise ValueError("Captured sides require different external release/revision pins")
        # The existing bootstrap lock owns checker-only packages; the stack lock owns
        # runtime packages. Their disjoint selection avoids inventing a merged pin roster.
        bootstrap = ResolvedLock.read(self.core / "uv.lock")
        checker_names = {entry.name for entry in bootstrap.package} - names - self.local_names
        checker_versions = {name: record.version for name, record in resolution.packages.items()
                            if name in checker_names}
        if bootstrap.pins(checker_versions, resolution.core_commit) != {
                name: resolution.packages[name].pin() for name in checker_versions}:
            raise ValueError("Preparing runtime sources changed a locked checker-only package")
        for side in Side.sides():
            for requirement in self.requirements(side):
                record = resolution.packages[canonicalize_name(requirement.name)]
                if not requirement.specifier.contains(record.version, prereleases=True):
                    raise ValueError(f"The common environment does not satisfy {requirement}")

    def prepare(self) -> RuntimeResolution:
        prefix = self.runtime.prefix.resolve()
        runner = Path(os.environ["RUNNER_TEMP"]).resolve()
        if (not self.source_directory.resolve().is_relative_to(runner) or not prefix.is_relative_to(runner)
                or not self.checkout.resolve().is_relative_to(self.source_directory.resolve())
                or not (prefix / "pyvenv.cfg").is_file()):
            raise ValueError("The common checker requires its isolated runner bootstrap prefix and input directory")
        self.require_uv()
        self.write_project()
        original = ResolvedLock.read(self.lock)
        self.resolve()
        # Retain the bootstrap's Core dev/checker packages in this SAME isolated prefix.
        self.commands.run(self.uv, "sync", "--project", self.checkout / "stack", "--locked",
                          "--inexact", "--no-editable", "--python", self.python,
                          env={**os.environ, "UV_PROJECT_ENVIRONMENT": str(self.runtime.prefix)})
        self.commands.run(self.uv, "pip", "check", "--python", self.python)
        commit = Repo.git(self.core, "rev-parse", "HEAD").strip()
        resolution = RuntimeResolution.capture(self, commit)
        self.require_resolution(resolution, original)
        resolution.save(self.runtime.prefix)
        digest = hashlib.sha256(self.lock.read_bytes()).hexdigest()
        bootstrap_digest = hashlib.sha256((self.core / "uv.lock").read_bytes()).hexdigest()
        print(f"CHECKER INTERPRETER {self.python}\nCHECKER LOCK {digest}\n"
              f"CHECKER BOOTSTRAP LOCK {bootstrap_digest}\n{resolution.model_dump_json(indent=2)}", flush=True)
        return resolution


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ci-inputs", type=Path, required=True)
    args = parser.parse_args()
    directory = args.ci_inputs.resolve()
    executable = StackSources.frontend()
    inputs = IntegrationInput.at(directory)
    with InstallCommands() as commands:
        CheckerStack(checkout=directory / "checker", commands=commands, home=directory,
                     runtime=LaunchRuntime(Path(sys.executable)), source_directory=directory,
                     inputs=inputs, uv_program=executable).prepare()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
