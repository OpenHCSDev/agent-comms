"""Separate productive settlement, the live successor lease, and its UI projection."""
from pathlib import Path
import json
from nominal_refactor_advisor.codemod import CodemodPlanSequence,CodemodSourceSnapshot,SourceRewriteTarget,PatchTargetOperation,SourceTextReplacement
from nominal_refactor_advisor.refactors.source_insertion import EnsureImportOperation
root=Path('/home/ts/wt/comms-friction--P23--toad')
file=root/'tests/native/test_native_goal_bar.py'
def t(name=None):return SourceRewriteTarget(file_path=str(file),qualname=name)
name='test_failed_ledger_repaints_retry_then_active_without_rewriting_goal'
replacements=(
 SourceTextReplacement('async def '+name+'(', 'async def test_failed_ledger_repaints_retry_then_its_live_successor_without_rewriting_goal('),
 SourceTextReplacement('    def reply(_request, number):\n', '    productive_turns: list[ActiveTurn] = []\n\n    def reply(_request, number):\n'),
 SourceTextReplacement('        if number == 2:\n', '        if number == 2:\n            productive_turns.append(comms.registry.require(session).turn_record.read().require_active())\n'),
 SourceTextReplacement('provider = ControlledProvider(reply=reply, request_budget=4)', 'provider = ControlledProvider(reply=reply, request_budget=4, expected_disconnects=frozenset({4}))'),
 SourceTextReplacement('''        await pilot.click(goal_control(bar, HistoryInteraction))
''','''        failed_snapshot = await agent.get_goal_snapshot()
        assert isinstance(failed_snapshot.execution, AttemptFailedExecution)
        await pilot.click(goal_control(bar, HistoryInteraction))
'''),
 SourceTextReplacement('''        await ui.until(lambda: "Active" in str(details.query_one(CurrentHeading).render()),
                       why="the already-open details following productive settlement")
''','''        await ui.until(provider.entered.is_set, seconds=30,
                       why="the next autonomous request is held at the provider")
        productive, = productive_turns
        resolved = comms.goals.open_goal_store().read_attempt(failed_snapshot.execution.evidence.attempt_id)
        assert resolved.resolved_as(ResolvedAttempt(), DecidedResolution(VerifiedTurnDecision(productive.id)))
        owner = comms.registry.require(session)
        held = owner.turn_record.read().require_active()
        assert held.id != productive.id and held.turn_generation != productive.turn_generation
        assert held.owner == owner.process_identity and held.owner.alive()
        observed = await agent.get_goal_snapshot()
        assert observed.goal == first and observed.execution == RunningExecution(first.id)
        assert owner.turn_record.read().require_active() == held
        await ui.until(lambda: details.execution == observed.execution
                       and details.goal_display == observed.goal,
                       why="the open details agree with the actual held successor snapshot")
        assert str(details.query_one(CurrentHeading).render()) == observed.goal.details_heading(observed.execution)
'''),
)
ops=(
 EnsureImportOperation(target=t(),import_source='from agent_comms.execution.turn_lease import ActiveTurn\nfrom agent_comms.planning.attempt_resolution import DecidedResolution, VerifiedTurnDecision\nfrom agent_comms.planning.goal_attempt_phase import ResolvedAttempt\nfrom agent_comms.planning.goal_presentation import AttemptFailedExecution, RunningExecution\n'),
 PatchTargetOperation(target=t(name),replacements=replacements,rationale='The productive turn resolves the exact failed attempt before a distinct autonomous successor becomes live. Assert the original ledger resolution, held successor identity and process, unchanged goal and exact owner snapshot before checking the existing Pause control and draft. Never label an executing owner idle.'),
)
helper=root/'tests/support/native_steps.py'
ops=(*ops,PatchTargetOperation(target=SourceRewriteTarget(file_path=str(helper),qualname='goal_on_real_owner'),replacements=(SourceTextReplacement('view.goal_display.snapshot == expected_goal','view.goal_display == expected_goal'),),rationale='Require the real available owner projection, not an unavailable wrapper that merely retains the same previous snapshot.'))
sequence=CodemodPlanSequence.from_operations(ops)
Path(__file__).with_suffix('.json').write_text(json.dumps(sequence.project_json_object(sequence),indent=2)+'\n')
sources={str(file):file.read_text(),str(helper):helper.read_text()}
simulation=sequence.simulate(CodemodSourceSnapshot.from_source_mapping(sources))
Path(__file__).with_suffix('.diff').write_text(simulation.unified_diff(sources))
print('Native successor/ledger proof plan simulated, no shared edits or tests',flush=True)
