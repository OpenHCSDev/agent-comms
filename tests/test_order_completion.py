"""One copied-bus command family owns check correction, delivery and closure."""
import shutil
import subprocess
import sys
import time
from dataclasses import replace

import pytest

from agent_comms.errors import TodoError
from agent_comms.execution.child_process import OrderCheckFailedError
from agent_comms.execution.launch_runtime import LaunchRuntime
from agent_comms.installer.build_identity import RecordedBuildIdentity
from agent_comms.installer.runtime_resolution import CorePackage, RuntimeResolution
from agent_comms.nominal.field_codec import FieldCodec, RecordProblems
from agent_comms.planning.goals import (
    CheckedCommit,
    CountCheckResult,
    OrderCheck,
)
from agent_comms.planning.order_runs import ApplicationOrderRun
from agent_comms.planning.orders import (
    CorrectCheckTodo,
    DeliveredProblemOrder,
    DoneTodoState,
    InstalledMergedProblemOrder,
    MergedProblemOrder,
    OperationDelivery,
    PendingProblemOrder,
    ProblemOrder,
    ProgressRead,
    ReportDelivery,
    ReportedProblemOrder,
    Todo,
    TriageScope,
)
from agent_comms.planning.progress_claims import InstalledClaim
from agent_comms.planning.pull_requests import PullRequestLink
from agent_comms.projects.checkouts import (
    Checkout,
    DetachedStart,
)
from agent_comms.projects.repo import Repo
from agent_comms.projects.work_branches import WorkBranches
from agent_comms.storage.recorded_time import RecordedEventTime
from agent_comms.team.order import Team, main
from agent_comms.team.standing import HolderGone
from agent_comms.ui_model.teammate_state import AwaitingApplicationRun, TeammateObservation
from team_heartbeat_fixture import capture_frame
from test_proof_build import ProofBuildCheckout

pytest_plugins = ('test_todos', 'test_held_problem_orders')


class TestOrderCompletion:
    @staticmethod
    def record(ctx: Team, order: ProblemOrder) -> Todo:
        store = ctx.comms.goals.todos
        current = store.get('P1')
        with store.writing() as connection:
            replace(current, order=order, revision=current.revision + 1).upsert(connection)
        return store.get('P1')

    def test_await_run_names_only_its_orders_and_yields_to_source_work(
        self, held_team, proof_build_checkouts,
    ):
        """2026-10-25: "AWAIT RUN unit-backend P483 P739" named P739, sent with no commit,
        and hid that its holder was still writing it."""
        ctx = held_team
        repo = ctx.repos[0]
        graph = next(graph for graph in proof_build_checkouts if graph.repository.github == repo.github)
        pending = ctx.comms.goals.todos.get('P1')
        run = ApplicationOrderRun("The real application shows the merged fix after a restart.")
        awaiting = self.record(ctx, replace(pending.order, run=run).merged(graph.installed))
        delivered = replace(pending, id='P2', order=pending.order.delivered(
            OperationDelivery(at=RecordedEventTime(time.time()))))
        writing = replace(pending, id='P3')

        def orders(*todos):
            teammate = TeammateObservation(ctx.comms, ctx.comms.registry.require('worker'), todos,
                                   capture_frame(ctx.comms, ProgressRead()),
                                   (), time.time(),
                                   ctx.comms.registry.snapshot().status('worker'), False)
            return [state.orders for state in AwaitingApplicationRun.derive(teammate)]

        assert orders(awaiting, delivered) == [('P1',)]
        assert orders(awaiting, delivered, writing) == []

    @pytest.fixture
    def passed_merged_order(
        self, held_team: Team, proof_build_checkouts: tuple[ProofBuildCheckout, ...],
    ) -> Todo:
        repo = held_team.repos[0]
        graph = next(item for item in proof_build_checkouts
                     if item.repository.github == repo.github)
        current = held_team.comms.goals.todos.get('P1')
        check = OrderCheck(repo.checkout,
                           "git log -1 --format=%s | grep -c 'proof 0'",
                           CountCheckResult(0))
        check.result.require(check.at(
            graph.installed, repo.checkout,
            held_team.comms.registry.storage_rule.checkout_base))
        return self.record(held_team, replace(
            current.order, check=check,
            pull_requests=(PullRequestLink(repo.github, 1),)).merged(graph.installed))

    @pytest.mark.parametrize('closed', (False, True), ids=('merged', 'installed-merged'))
    def test_recorded_source_claim_accepts_the_merged_family(
        self, held_team: Team, passed_merged_order: Todo, closed: bool,
    ) -> None:
        ctx = held_team
        artifact = ctx.root / 'installed-source.txt'
        works = 'The copied application keeps its checked source result available.'
        artifact.write_text(works)
        if closed:
            main(['close', 'P1', '--works', works, '--artifact', str(artifact)], ctx)
        before = ctx.comms.goals.todos.get('P1')
        assert InstalledClaim('P1').verify(ctx.comms, ctx.repos[0].checkout) == ()
        assert ctx.comms.goals.todos.get('P1') == before

    @pytest.mark.parametrize('command', ('printf 1', 'printf unproved'))
    def test_recorded_source_claim_refuses_invalid_results(
        self, held_team: Team, passed_merged_order: Todo, command: str,
    ) -> None:
        ctx = held_team
        original = passed_merged_order.order
        check = OrderCheck(original.check.directory, command, CountCheckResult(0))
        check.at(original.check_commit, check.directory,
                 ctx.comms.registry.storage_rule.checkout_base)
        merged = replace(original, check=check)
        for order in (merged, merged.installed('Copied application proof', str(ctx.root))):
            with pytest.raises((TodoError, OrderCheckFailedError)):
                order.require_checked(passed_merged_order.id)

    @pytest.mark.parametrize('unrecorded', (
        lambda order: replace(order, check=replace(order.check, command='printf 0')),
        lambda order: replace(order, check=replace(
            order.check, command=order.check.command + ' ')),
        lambda order: replace(order, sha=Repo.git(
            order.check.directory, 'rev-parse', f'{order.check_commit}^1').strip()),
    ), ids=('missing', 'wrong-command', 'wrong-commit'))
    def test_recorded_source_claim_never_runs_missing_evidence(
        self, held_team: Team, passed_merged_order: Todo, unrecorded,
    ) -> None:
        merged = unrecorded(passed_merged_order.order)
        before = CheckedCommit.stored(merged.check.directory, merged.check_commit)
        for order in (merged, merged.installed('Copied application proof', str(held_team.root))):
            with pytest.raises(TodoError, match='its order has none'):
                order.require_checked(passed_merged_order.id)
        assert CheckedCommit.stored(merged.check.directory, merged.check_commit) == before

    @pytest.mark.parametrize('commands', ((), (('done', 'worker', 'P1'),)))
    @pytest.mark.parametrize('command', (
        'printf 1',
        "if git log -1 --format=%s | grep -q 'proof 0'; "
        "then printf 1; else printf unproved; fi",
    ))
    def test_failed_corrected_source_returns_to_work(
        self, held_team: Team, passed_merged_order: Todo,
        commands: tuple[tuple[str, ...], ...], command: str,
    ) -> None:
        ctx = held_team
        for completion in commands:
            main(list(completion), ctx)
        before = ctx.comms.goals.todos.get(passed_merged_order.id)
        repo = ctx.repos[0]
        main(['check', 'P1', '--check', f'{repo.checkout} :: {command} -> 0',
              '--why', 'The corrected criterion must hold on the recorded source too'], ctx)
        after = ctx.comms.goals.todos.get(before.id)
        assert isinstance(after.order, PendingProblemOrder)
        assert not after.order.closed_by_merge(
            after, ProgressRead(), ctx.comms.registry.storage_rule.checkout_base)
        original = passed_merged_order.order.checked()
        retained_source = original.merged_link(original.pull_requests[0])
        assert after.order.corrections[-2].previous == retained_source
        assert (after.order.id, after.assignment, after.review_at, after.strike_count) == (
            before.order.id, before.assignment, before.review_at, before.strike_count)
        assert after.order.check.command == command
        correction = after.order.corrections[-1]
        assert correction.previous == before.order.check
        wire = FieldCodec.encode(correction)
        decoded = FieldCodec.decode(CorrectCheckTodo, wire)
        assert decoded == correction
        with pytest.raises(TodoError, match='Prepare the corrected order check'):
            decoded.replaced(before.order)

    def test_valid_delivered_correction_keeps_the_delivery(
        self, held_team: Team, passed_merged_order: Todo,
    ) -> None:
        ctx = held_team
        main(['done', 'worker', 'P1'], ctx)
        before = ctx.comms.goals.todos.get(passed_merged_order.id)
        assert isinstance(before.order, DeliveredProblemOrder)
        repo = ctx.repos[0]
        command = "git log -1 --format=%s | grep -c 'proof 0$'"
        main(['check', 'P1', '--check', f'{repo.checkout} :: {command} -> 0',
              '--why', 'The corrected criterion still holds on the delivered source'], ctx)
        after = ctx.comms.goals.todos.get(before.id)
        assert isinstance(after.order, DeliveredProblemOrder)
        assert after.order.delivery == before.order.delivery
        assert (after.order.id, after.assignment, after.review_at, after.strike_count) == (
            before.order.id, before.assignment, before.review_at, before.strike_count)
        assert after.order.check.command == command

    def test_merged_correction_and_done_use_the_current_check(
        self, held_team, proof_build_checkouts,
    ):
        ctx = held_team
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

    def test_open_correction_uses_main_and_keeps_the_order(self, held_team):
        ctx = held_team
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

    def test_triage_done_reads_the_report_without_a_sha(self, held_team):
        ctx = held_team
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

    def test_done_reports_missing_evidence_together_and_keeps_the_reused_checkout(
        self, held_team: Team,
    ) -> None:
        """Done names every missing piece of evidence at once. An owner keeps one checkout per
        repository across orders (StorageRule.reused_checkout), so the order's checkout still
        existing is no refusal."""
        ctx = held_team
        original = ctx.comms.goals.todos.get('P1')
        current = self.record(ctx, replace(original.order, run=ApplicationOrderRun(
            'The application displays the completed work and keeps its evidence visible')))
        rule = ctx.comms.registry.storage_rule
        worker = ctx.comms.registry.require('worker')
        path = rule.order_checkout(rule.roots_of(worker), current.id)
        checkout = Checkout.add(ctx.repos[0].checkout, path, DetachedStart('HEAD'))
        try:
            with pytest.raises(RecordProblems) as refused:
                main(['done', 'worker', 'P1', '--op'], ctx)
            assert refused.value.problems.keys() >= {'--seen.run', '--seen.working'}
            assert 'checkout' not in refused.value.problems
            assert ctx.comms.goals.todos.get('P1') == current
            launcher = shutil.which('agent-comms-team')
            assert launcher is not None
            result = subprocess.run(
                [sys.executable, launcher,
                 '--root', str(ctx.root), 'order', 'done', 'worker', 'P1', '--op', '--as', 'colleague'],
                cwd=ctx.root, capture_output=True, text=True, timeout=30,
            )
            assert result.returncode == 1, result.stdout + result.stderr
            assert all(field in result.stderr for field in ('--seen.run', '--seen.working')), result.stderr
            assert ctx.comms.goals.todos.get('P1') == current
        finally:
            checkout.retire()

    def test_operation_done_keeps_its_existing_evidence(self, held_team):
        ctx = held_team
        evidence = ctx.root / 'operation.txt'
        evidence.write_text('The copied operation completed through its current check.')
        main(['done', 'worker', 'P1', '--op', '--seen', str(evidence)], ctx)
        delivered = ctx.comms.goals.todos.get('P1')
        assert isinstance(delivered.order.delivery, OperationDelivery)
        assert delivered.order.delivery.evidence.artifact() == str(evidence)

    def test_delivered_work_can_be_proved_again_on_a_newer_build(
        self, held_team, proof_build_checkouts,
    ):
        ctx = held_team
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

    def test_triage_rejects_an_operation_without_recording(self, held_team):
        ctx = held_team
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
        self, held_team, proof_build_checkouts, holder,
    ):
        ctx = held_team
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
        assert HolderGone.violations(ctx.comms) == ()
