"""Reviewed runtime action within the original all-stopped installation."""
from abc import abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from agent_comms.declared_family import DeclaredFamily
from agent_comms.field_codec import PathText
from publish_openhcs_recovery import retain_file
from retained_summary_reset import AcquiredRuntimeFiles
from native_schema_carry import NativeSchemaCarryPlan, NativeSchemaDeclaration, RuntimeNativeFiles


@dataclass(frozen=True)
class RuntimeInstallation(DeclaredFamily, affix='RuntimeInstallation'):
    # Frozen whole-family source declaration, captured by the authentic writer.
    # No table roster, target DDL, reason cases or version shortcut live here.
    goal_schema: dict[str, str]

    def synchronize_goal(self, acquired, destination):
        return NativeSchemaDeclaration.observe().synchronize_goal(acquired, destination, self.goal_schema)

    def unchanged_protected(self, paths: frozenset[Path], acquired: AcquiredRuntimeFiles) -> frozenset[Path]:
        """The member owns which original bytes its installation may change."""
        return frozenset(paths)

    @abstractmethod
    def retain_protected(self, paths: frozenset[Path], directory: Path): ...

    @abstractmethod
    def install(self, acquired: AcquiredRuntimeFiles, destination: Path): ...


@dataclass(frozen=True)
class ResetRuntimeInstallation(RuntimeInstallation):
    def retain_protected(self, paths: frozenset[Path], directory: Path):
        return [retain_file(path, directory / f'protected-{index}')
                for index, path in enumerate(sorted(paths))]

    def install(self, acquired, destination):
        return acquired.retain_and_remove(destination)


@dataclass(frozen=True)
class PreserveRuntimeInstallation(RuntimeInstallation):
    def retain_protected(self, paths: frozenset[Path], directory: Path):
        # Original protected bytes are hashed by the installation before/after.
        # The earlier reset's private preimages remain at their original paths.
        return []

    def install(self, acquired, destination):
        acquired.require_original()
        return {'classification': 'runtime/preserve', 'original_files': acquired.evidence(),
                'retired': [], 'copied_bytes': 0}


@dataclass(frozen=True)
class CarryNativeRuntimeInstallation(PreserveRuntimeInstallation):
    """Native6 declarations; original compaction proof facts remain unchanged."""

    original: NativeSchemaDeclaration
    source_python: Annotated[Path, PathText]
    candidate: Annotated[Path, PathText]

    def unchanged_protected(self, paths: frozenset[Path], acquired: AcquiredRuntimeFiles) -> frozenset[Path]:
        return super().unchanged_protected(paths, acquired).difference(
            RuntimeNativeFiles(acquired.paths[0].parent).paths)

    def install(self, acquired, destination):
        acquired.require_original()
        original_evidence = acquired.evidence()
        # The publisher has stopped its original audience and holds the wire
        # custody before invoking this member. No live-store plan is admitted.
        plan = NativeSchemaCarryPlan.prepare(acquired.paths[0].parent, self.candidate,
                       self.original, self.source_python)
        carried = plan.install(destination)
        changed = {plan.root / item.name for item in plan.stores}
        acquired.unchanged_by(changed).require_original()
        return {**carried, 'compaction_original_files': original_evidence}
