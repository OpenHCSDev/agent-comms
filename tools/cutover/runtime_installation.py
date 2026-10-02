"""Reviewed runtime action within the original all-stopped installation."""
from abc import abstractmethod
from dataclasses import dataclass
from pathlib import Path

from agent_comms.declared_family import DeclaredFamily
from publish_openhcs_recovery import retain_file
from retained_summary_reset import AcquiredRuntimeFiles
from native_schema_carry import NativeSchemaCarryPlan, NativeSchemaDeclaration


@dataclass(frozen=True)
class RuntimeInstallation(DeclaredFamily, affix='RuntimeInstallation'):
    # Frozen whole-family source declaration, captured by the authentic writer.
    # No table roster, target DDL, reason cases or version shortcut live here.
    goal_schema: dict[str, str]

    def synchronize_goal(self, acquired, destination):
        return NativeSchemaDeclaration.observe().synchronize_goal(acquired, destination, self.goal_schema)

    def unchanged_protected(self, paths: frozenset[Path]) -> frozenset[Path]:
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

    plan: NativeSchemaCarryPlan

    def unchanged_protected(self, paths: frozenset[Path]) -> frozenset[Path]:
        self.plan.require_candidate()
        return super().unchanged_protected(paths).difference(
            self.plan.root / item.name for item in self.plan.stores)

    def install(self, acquired, destination):
        if acquired.paths[0].parent != self.plan.root:
            raise ValueError('Native carry names another stopped root')
        acquired.require_original()
        original_evidence = acquired.evidence()
        carried = self.plan.install(destination)
        changed = {self.plan.root / item.name for item in self.plan.stores}
        acquired.unchanged_by(changed).require_original()
        return {**carried, 'compaction_original_files': original_evidence}
