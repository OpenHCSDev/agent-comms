"""A goal journey yields only after its original owner projection mounts its controls."""
from pathlib import Path
import json
from nominal_refactor_advisor.codemod import CodemodPlanSequence,CodemodSourceSnapshot,SourceRewriteTarget,PatchTargetOperation,SourceTextReplacement
root=Path('/home/ts/wt/comms-friction--P23--toad')
old='''    async with native_session(workspace, prepare=prepare, size=size, provider=provider) as native:
        yield native
'''
new='''    async with native_session(workspace, prepare=prepare, size=size, provider=provider) as native:
        expected_goal = native.comms.registry.require(native.thread).goal
        view = native.ui.conversation
        await native.ui.until(
            lambda: view.goal_display.snapshot == expected_goal and view.goal_bar.controls.is_mounted,
            why="the owner's current goal and its mounted controls",
        )
        yield native
'''
operation=PatchTargetOperation(target=SourceRewriteTarget(file_path=str(root/'tests/support/native_steps.py'),qualname='goal_on_real_owner'),replacements=(SourceTextReplacement(old,new),),rationale='ACP session connection does not imply the separate owner-goal observation has arrived or lazy goal controls have mounted. Preserve the actual tests and wait on their existing source and widget owners before yielding the arranged native journey.')
sequence=CodemodPlanSequence.from_operations((operation,))
Path(__file__).with_suffix('.json').write_text(json.dumps(sequence.project_json_object(sequence),indent=2)+'\n')
sources={str(root/'tests/support/native_steps.py'):(root/'tests/support/native_steps.py').read_text()}
simulation=sequence.simulate(CodemodSourceSnapshot.from_source_mapping(sources))
Path(__file__).with_suffix('.diff').write_text(simulation.unified_diff(sources))
print('One native fixture-readiness operation simulated; original assertions untouched, no source mutation or test run')
