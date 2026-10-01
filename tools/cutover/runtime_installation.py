"""Reviewed runtime action within the original all-stopped installation."""
from abc import abstractmethod
from dataclasses import dataclass
from pathlib import Path

from agent_comms.declared_family import DeclaredFamily
from publish_openhcs_recovery import retain_file
from retained_summary_reset import AcquiredRuntimeFiles
from native_schema_carry import NativeSchemaCarryPlan


class RuntimeInstallation(DeclaredFamily, affix='RuntimeInstallation'):
    def unchanged_protected(self, paths: set[Path]) -> set[Path]:
        """The member owns which original bytes its installation may change."""
        return paths

    @abstractmethod
    def retain_protected(self, paths: set[Path], directory: Path): ...

    @abstractmethod
    def install(self, acquired: AcquiredRuntimeFiles, destination: Path): ...


@dataclass(frozen=True)
class ResetRuntimeInstallation(RuntimeInstallation):
    def retain_protected(self, paths, directory):
        return [retain_file(path, directory / f'protected-{index}')
                for index, path in enumerate(sorted(paths))]

    def install(self, acquired, destination):
        return acquired.retain_and_remove(destination)


@dataclass(frozen=True)
class PreserveRuntimeInstallation(RuntimeInstallation):
    def retain_protected(self, paths, directory):
        # Original protected bytes are hashed by the installation before/after.
        # The earlier reset's private preimages remain at their original paths.
        return []

    def install(self, acquired, destination):
        acquired.require_original()
        return {'classification': 'runtime/preserve', 'original_files': acquired.evidence(),
                'retired': [], 'copied_bytes': 0}


@dataclass(frozen=True)
class CarryNativeRuntimeInstallation(PreserveRuntimeInstallation):
    """Changed native declarations; compaction remains original-format bytes."""

    plan: NativeSchemaCarryPlan

    def unchanged_protected(self, paths):
        self.plan.require_candidate()
        return paths - {self.plan.root / item.name for item in self.plan.stores}

    def install(self, acquired, destination):
        if acquired.paths[0].parent != self.plan.root:
            raise ValueError('Native carry names another stopped root')
        preserved = super().install(acquired, destination)
        carried = self.plan.install(destination)
        acquired.require_original()
        return {**carried, 'compaction': preserved}
