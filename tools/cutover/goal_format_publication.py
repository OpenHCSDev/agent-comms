"""Goal format boundary on the existing original-stop / target-launch handoff.

Original Thread values cross as external JSON, not target-decoded credentials.
Owner identities and the inherited wire descriptor retain their existing owners.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import sys
from typing import Annotated

from pydantic import JsonValue

from agent_comms.field_codec import FieldCodec, PathText
from agent_comms.owner_cutover import PreserveOwnerRuntime
from agent_comms.owner_lifecycle import OwnerRestartResult, OwnerRestartSelection
from agent_comms.owner_restart import OwnerRestartHandoff, StoppedOwnerBatch
from agent_comms.threads import Thread
from cutover_child import run_cutover_child
from native_schema_carry import GoalFormatCarryPlan
from publish_retained_summary import PublishRetainedSummary, ReviewedInstalledRuntime, publish_original
from runtime_installation import CarryGoalFormatsRuntimeInstallation


@dataclass(frozen=True)
class GoalPublicationReview:
    cohort: ReviewedInstalledRuntime
    installation: CarryGoalFormatsRuntimeInstallation
    receipt: Annotated[Path, PathText]

    def run(self):
        if sys.executable != str(self.installation.source_python):
            raise ValueError('Original Goal stopping requires its reviewed source decoder')
        self.cohort.require_artifacts()
        return publish_original(self.cohort, PreserveOwnerRuntime(), self.installation,
                                self.receipt, operation_type=OriginalGoalPublication)


@dataclass(frozen=True)
class OriginalGoalPublication(PublishRetainedSummary):
    runtime_installation: CarryGoalFormatsRuntimeInstallation

    def require_cohort(self):
        if sys.executable != str(self.runtime_installation.source_python):
            raise ValueError('Original Goal stopping changed source decoder')
        self.cohort.require_artifacts()

    def recover(self, stopped):
        if self.receipt.with_suffix('.goal-formats').exists():
            raise RuntimeError('Goal carry preimages exist; remain stopped without an original decoder')
        return super().recover(stopped)

    def complete(self, stopped):
        self.acquire_stopped_route()
        environment = dict(os.environ)
        environment.pop('PYTHONPATH', None)
        prepared = run_cutover_child(
            [str(self.cohort.target / 'bin/python'),
             str(Path(__file__).with_name('native_schema_carry.py')),
             '--goal-format-candidate', str(self.runtime_installation.candidate),
             '--source-root', str(self.cohort.root),
             '--source-python', str(self.runtime_installation.source_python)],
            environment=environment, packet='',
        )
        plan = FieldCodec.decode(GoalFormatCarryPlan, json.loads(prepared.stdout))
        # Only the original, validated stopped owner publishes. The target
        # codec prepared the candidate without opening a target Registration.
        installed = plan.install(stopped, self.receipt.with_suffix('.goal-formats'))
        self.note('goal-formats-installed-before-target-registry-read',
                  goal_format_installation=installed)
        packet = StoppedGoalPublication(
            GoalPublicationReview(self.cohort, self.runtime_installation, self.receipt),
            self.audience, tuple(FieldCodec.encode(thread) for thread in self.originals),
            stopped.handoff, plan,
        )
        result = run_cutover_child(
            [str(self.cohort.target / 'bin/python'),
             str(Path(__file__).with_name('finish_goal_publication.py')),
             str(stopped.wire_descriptor), str(self.route_directory)],
            environment=environment, packet=json.dumps(FieldCodec.encode(packet)),
            descriptors=(stopped.wire_descriptor, self.route_directory),
        )
        return FieldCodec.decode(tuple[OwnerRestartResult, ...], json.loads(result.stdout))


@dataclass(frozen=True)
class StoppedGoalPublication:
    review: GoalPublicationReview
    audience: tuple[OwnerRestartSelection, ...]
    originals: tuple[dict[str, JsonValue], ...]
    handoff: OwnerRestartHandoff
    plan: GoalFormatCarryPlan

    def finish(self, wire_descriptor: int, route_descriptor: int):
        from agent_comms.active_route import active_route_path, acquire_exclusive_route_lock
        from agent_comms.message_bus import MessageBus
        from agent_comms.owner_lifecycle import OwnerLifecycle
        from agent_comms.registration import Registration

        cohort, installation = self.review.cohort, self.review.installation
        cohort.require_original()
        opened, named = os.fstat(route_descriptor), active_route_path().parent.stat()
        if (opened.st_dev, opened.st_ino, opened.st_uid) != (named.st_dev, named.st_ino, os.geteuid()):
            raise ValueError('Inherited route descriptor differs from the reviewed route')
        acquire_exclusive_route_lock(route_descriptor)
        self.plan.verify_installed(self.review.receipt.with_suffix('.goal-formats'))
        # Registration decodes every retained Goal, so it comes after carry.
        registry = Registration(cohort.root / 'registry.json')
        lifecycle = OwnerLifecycle(cohort.root, registry, MessageBus(cohort.root / 'bus.jsonl', registry))
        stopped = StoppedOwnerBatch.accept(lifecycle, wire_descriptor, self.handoff)
        for thread in self.originals:
            self.plan.source.goal(thread['goal'])
        originals = tuple(FieldCodec.decode(Thread, thread) for thread in self.originals)
        operation = PublishRetainedSummary(cohort, self.audience, originals,
                                          PreserveOwnerRuntime(), installation,
                                          route_descriptor, self.review.receipt)
        # The original parent owns failure disposition. Converted bytes cannot
        # be restored through an original decoder or an implicit rollback.
        return operation.complete(stopped)
