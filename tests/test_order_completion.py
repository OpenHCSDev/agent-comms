"""One copied-bus command family owns check correction, delivery and closure."""
import time
from dataclasses import replace

import pytest

from agent_comms.build_identity import RecordedBuildIdentity
from agent_comms.commander.order import main
from agent_comms.commander.standing import HolderGone
from agent_comms.goals import CountCheckResult, OrderCheck
from agent_comms.installer.runtime_resolution import CorePackage, RuntimeResolution
from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.order_runs import ApplicationOrderRun
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.todos import (
    DeliveredProblemOrder,
    DoneTodoState,
    InstalledMergedProblemOrder,
    MergedProblemOrder,
    OperationDelivery,
    ProgressRead,
    PullRequestLink,
    ReportDelivery,
    ReportedProblemOrder,
    TriageScope,
)
from agent_comms.ui_model.unit_state import AwaitingApplicationRun, UnitObservation
from agent_comms.work_branches import WorkBranches

pytest_plugins = ('test_todos', 'test_held_problem_orders')


class TestOrderCompletion:
    @staticmethod
    def record(ctx, order):
        store = ctx.comms.goals.todos
        current = store.get('P1')
        with store.writing() as connection:
            replace(current, order=order, revision=current.revision + 1).upsert(connection)
        return store.get('P1')

    def test_await_run_names_only_its_orders_and_yields_to_source_work(
        self, held_commander, proof_build_checkouts,
    ):
        """2026-10-25: "AWAIT RUN unit-backend P483 P739" named P739, sent with no commit,
        and hid that the unit was still writing it."""
        ctx = held_commander
        repo = ctx.repos[0]
        graph = next(graph for graph in proof_build_checkouts if graph.repository.github == repo.github)
        pending = ctx.comms.goals.todos.get('P1')
        run = ApplicationOrderRun("The real application shows the merged fix after a restart.")
        awaiting = self.record(ctx, replace(pending.order, run=run).merged(graph.installed))
        delivered = replace(pending, id='P2', order=pending.order.delivered(
            OperationDelivery(at=RecordedEventTime(time.time()))))
        writing = replace(pending, id='P3')

        def orders(*todos):
            unit = UnitObservation(ctx.comms, ctx.comms.registry.require('worker'), todos,
                                   ProgressRead(), (), time.time(),
                                   ctx.comms.registry.snapshot().status('worker'), False)
            return [state.orders for state in AwaitingApplicationRun.derive(unit)]

        assert orders(awaiting, delivered) == [('P1',)]
        assert orders(awaiting, delivered, writing) == []

    def test_merged_correction_and_done_use_the_current_check(
        self, held_commander, proof_build_checkouts,
    ):
        ctx = held_commander
        repo = ctx.repos[0]
        graph = next(graph for graph in proof_build_checkouts if graph.repository.github == repo.github)
        original = ctx.comms.goals.todos.get('P1').order
        merged = replace(original, check=OrderCheck(
            repo.checkout, 'printf 1', CountCheckResult(0)))
        self.record(ctx, replace(merged, pull_requests=(PullRequestLink(repo.github, 1),))
                    .merged(graph.installed))
        command = "git log -1 --format=%s | grep -c 'proof 0'"
        main(['check', 'P1', '--check', f'{repo.checkout} :: {command} -> 0',
              '--why', 'The old check included declarations outside the authorized family'], ctx)
        corrected = ctx.comms.goals.todos.get('P1')
        assert isinstance(corrected.order, MergedProblemOrder)
        assert corrected.order.check_base(repo.checkout) == f'{graph.installed}^1'
        assert corrected.order.corrections[-1].previous == merged.check
        main(['done', 'worker', 'P1'], ctx)
        delivered = ctx.comms.goals.todos.get('P1')
        assert isinstance(delivered.order, DeliveredProblemOrder)
        assert delivered.order.check.command == command
        assert delivered.order.delivery.delivered == graph.installed
        assert delivered.order.check_base(repo.checkout) == repo.git(
            repo.checkout, 'rev-parse', f'{graph.installed}^1').strip()
        assert delivered.strike_count == corrected.strike_count

    def test_open_correction_uses_main_and_keeps_the_order(self, held_commander):
        ctx = held_commander
        original = ctx.comms.goals.todos.get('P1')
        repo = ctx.repos[0]
        main(['check', 'P1', '--check', f'{repo.checkout} :: printf 1 -> 0',
              '--why', 'The original check counted source outside the declared fix family'], ctx)
        corrected = ctx.comms.goals.todos.get('P1')
        assert corrected.order.check_base(repo.checkout) == f"origin/{WorkBranches.main}"
        assert corrected.order.check.command == 'printf 1'
        assert (corrected.order.id, corrected.assignment,
                corrected.review_at, corrected.strike_count) == (
                    original.order.id, original.assignment, original.review_at, original.strike_count)

    def test_triage_done_reads_the_report_without_a_sha(self, held_commander):
        ctx = held_commander
        self.record(ctx, replace(ctx.comms.goals.todos.get('P1').order, scope=TriageScope()))
        report = ctx.root / 'triage.txt'
        report.write_text(
            'The source check belongs to its order state; no implementation was requested.')
        main(['done', 'worker', 'P1', '--seen', str(report)], ctx)
        closed = ctx.comms.goals.todos.get('P1')
        assert closed.state is DoneTodoState
        assert isinstance(closed.order, ReportedProblemOrder)
        assert closed.order.delivery.report == report.read_text()
        assert closed.assignment is None

    def test_operation_done_keeps_its_existing_evidence(self, held_commander):
        ctx = held_commander
        evidence = ctx.root / 'operation.txt'
        evidence.write_text('The copied operation completed through its current check.')
        main(['done', 'worker', 'P1', '--op', '--seen', str(evidence)], ctx)
        delivered = ctx.comms.goals.todos.get('P1')
        assert isinstance(delivered.order.delivery, OperationDelivery)
        assert delivered.order.delivery.evidence.artifact() == str(evidence)

    def test_delivered_work_can_be_proved_again_on_a_newer_build(
        self, held_commander, proof_build_checkouts,
    ):
        ctx = held_commander
        repo = ctx.repos[0]
        graph = next(graph for graph in proof_build_checkouts
                     if graph.repository.github == repo.github)
        original = ctx.comms.goals.todos.get('P1').order
        self.record(ctx, replace(original, pull_requests=(PullRequestLink(repo.github, 1),))
                    .merged(graph.installed))
        previous_evidence = ctx.root / 'previous-proof.txt'
        previous_evidence.write_text('The delivered work passed on the previous build.')
        previous_build = RecordedBuildIdentity.installed()
        main(['done', 'worker', 'P1', '--seen', str(previous_evidence)], ctx)
        previous = ctx.comms.goals.todos.get('P1').order.delivery
        assert previous.proof_build == previous_build

        core = next(graph for graph in proof_build_checkouts
                    if graph.repository.github == CorePackage.repository)
        runtime = LaunchRuntime.installed()
        resolution = RuntimeResolution.at(runtime.prefix)
        resolution.model_copy(update={'core_commit': core.newer}).save(runtime.prefix)
        current_build = RecordedBuildIdentity.installed()
        assert current_build != previous_build
        current_evidence = ctx.root / 'current-proof.txt'
        current_evidence.write_text('The same delivered work passed on the current build.')
        main(['done', 'worker', 'P1', '--seen', str(current_evidence)], ctx)
        current = ctx.comms.goals.todos.get('P1').order
        assert current.delivery.proof_build == current_build
        assert current.delivery.evidence.artifact() == str(current_evidence)
        assert current.delivery.commit == previous.commit
        assert current.delivery.delivered == previous.delivered
        assert current.delivered(current.delivery) == current

        different_work = replace(current.delivery, delivered_commit=graph.newer)
        with pytest.raises(ValueError, match=r'^The order already records a different delivery\.$'):
            current.delivered(different_work)
        with pytest.raises(ValueError, match=r'^The order already records a different delivery\.$'):
            current.delivered(ReportDelivery(report="A report is different delivered work."))

    def test_triage_rejects_an_operation_without_recording(self, held_commander):
        ctx = held_commander
        original = self.record(
            ctx, replace(ctx.comms.goals.todos.get('P1').order, scope=TriageScope()))
        with pytest.raises(ValueError, match='Triage returns --seen REPORT'):
            main(['done', 'worker', 'P1', '--op'], ctx)
        assert ctx.comms.goals.todos.get('P1') == original

    @pytest.mark.parametrize('holder', [
        lambda registry: None,
        lambda registry: (registry.unregister('worker'), registry.remove('worker')),
    ], ids=['registered', 'removed'])
    def test_merged_close_accepts_the_explicit_artifact(
        self, held_commander, proof_build_checkouts, holder,
    ):
        ctx = held_commander
        holder(ctx.comms.registry)
        graph = next(graph for graph in proof_build_checkouts
                     if graph.repository.github == ctx.repos[0].github)
        works = 'Open retained workflow and confirm its report stays visible'
        original = ctx.comms.goals.todos.get('P1').order
        self.record(ctx, replace(original, run=ApplicationOrderRun(works)).merged(graph.installed))
        artifact = ctx.root / 'application.txt'
        artifact.write_text(works)
        main(['close', 'P1', '--works', works, '--artifact', str(artifact)], ctx)
        closed = ctx.comms.goals.todos.get('P1')
        assert closed.state is DoneTodoState
        assert isinstance(closed.order, InstalledMergedProblemOrder)
        assert closed.order.artifact == str(artifact)
        assert HolderGone.violations(ctx.comms, ctx.commander) == ()
