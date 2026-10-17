"""Reviewed runtime action within the original all-stopped installation."""
from abc import abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from agent_comms.declared_family import DeclaredFamily
from agent_comms.field_codec import PathText
from publish_openhcs_recovery import retain_file
from retained_summary_reset import AcquiredRuntimeFiles
from native_schema_carry import GoalNativeStore, NativeSchemaCarryPlan, NativeSchemaDeclaration, RuntimeNativeFiles


@dataclass(frozen=True)
class RuntimeInstallation(DeclaredFamily, affix='RuntimeInstallation'):
    @abstractmethod
    def original_goal(self) -> dict[str, str]: ...

    def synchronize_goal(self, acquired, destination):
        return GoalNativeStore.synchronize(acquired, destination, self.original_goal())

    def publish(self, cohort, task_carry, receipt):
        from publish_retained_summary import publish_original
        return publish_original(cohort, task_carry, self, receipt)

    def unchanged_protected(self, paths: frozenset[Path], acquired: AcquiredRuntimeFiles) -> frozenset[Path]:
        """The member owns which original bytes its installation may change."""
        return frozenset(paths)

    def retain_protected(self, paths: frozenset[Path], directory: Path):
        # Preserving installations authenticate originals without copying them.
        return []

    @abstractmethod
    def install(self, acquired: AcquiredRuntimeFiles, destination: Path): ...


@dataclass(frozen=True)
class PreserveRuntimeInstallation(RuntimeInstallation):
    goal_schema: dict[str, str]

    def original_goal(self):
        return self.goal_schema

    def install(self, acquired, destination):
        acquired.require_original()
        return {'classification': 'runtime/preserve', 'original_files': acquired.evidence(),
                'retired': [], 'copied_bytes': 0}


@dataclass(frozen=True)
class CarryGoalFormatsRuntimeInstallation(PreserveRuntimeInstallation):
    """Stop with the source codec; transfer the existing handoff to the target."""

    source_python: Annotated[Path, PathText]
    candidate: Annotated[Path, PathText]

    def publish(self, cohort, task_carry, receipt):
        import json
        import os
        from agent_comms.field_codec import FieldCodec
        from agent_comms.owner_cutover import PreserveOwnerRuntime
        from agent_comms.owner_lifecycle import OwnerRestartResult
        from cutover_child import run_cutover_child
        from goal_format_publication import GoalPublicationReview

        if type(task_carry) is not PreserveOwnerRuntime:
            raise ValueError('Goal format publication cannot compose an unrelated task conversion')
        if self.source_python != cohort.current_prefix / 'bin/python':
            raise ValueError('Goal source decoder differs from the reviewed original installation')
        review = GoalPublicationReview(cohort, self, receipt)
        environment = dict(os.environ)
        environment.pop('PYTHONPATH', None)
        result = run_cutover_child(
            [str(self.source_python), str(Path(__file__).with_name('start_goal_publication.py'))],
            environment=environment, packet=json.dumps(FieldCodec.encode(review)))
        return FieldCodec.decode(tuple[OwnerRestartResult, ...], json.loads(result.stdout))


@dataclass(frozen=True)
class ResetRuntimeInstallation(PreserveRuntimeInstallation):
    def retain_protected(self, paths: frozenset[Path], directory: Path):
        return [retain_file(path, directory / f'protected-{index}')
                for index, path in enumerate(sorted(paths))]

    def install(self, acquired, destination):
        return acquired.retain_and_remove(destination)


@dataclass(frozen=True)
class CarryNativeRuntimeInstallation(RuntimeInstallation):
    """Declared stopped carry; original compaction proof facts remain unchanged."""

    original: NativeSchemaDeclaration
    source_python: Annotated[Path, PathText]
    candidate: Annotated[Path, PathText]

    def original_goal(self):
        return self.original.goal

    def unchanged_protected(self, paths: frozenset[Path], acquired: AcquiredRuntimeFiles) -> frozenset[Path]:
        return super().unchanged_protected(paths, acquired).difference(
            RuntimeNativeFiles(acquired.paths[0].parent).paths)

    def install(self, acquired, destination):
        acquired.require_original()
        original_evidence = acquired.evidence()
        # The publisher has stopped its original audience and holds the wire
        # custody before invoking this member. No live-store plan is admitted.
        plan = NativeSchemaCarryPlan.prepare(
            acquired.paths[0].parent, self.candidate, self.original, self.source_python)
        carried = plan.install(destination)
        changed = {plan.root / item.name for item in plan.stores}
        acquired.unchanged_by(changed).require_original()
        return {**carried, 'compaction_original_files': original_evidence}
