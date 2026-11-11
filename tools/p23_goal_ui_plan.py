"""Prepare the original goal interaction family dependency cut for NRA."""
import json
from pathlib import Path

ROOT = Path('/home/ts/wt/comms-friction--P23--toad')
OUT = Path('/home/ts/.cache/agent-scratch/comms-friction/p23-goal-ui-plan.json')


def patch(path, changes, target=None):
    operation = {'operation': 'patch_target', 'file_path': str(ROOT / path), 'replacements': [{'old_source': old, 'new_source': new} for old, new in changes]}
    if target:
        operation['target_qualname'] = target
    return operation


view = '''"""The original view capabilities consumed by source-bound goal controls."""

from pathlib import Path

from agent_comms.goal_presentation import GoalExecutionControls
from agent_comms.ui_model.goal import GoalDisplay
from textual.widget import Widget
from toad.agent_binding import AgentConnection
from toad.app_node import ToadNode
from toad.core_event_carrier import CoreEventReceiver
from toad.session_observation import GoalObservation
from toad.widgets.flash import Flash


class GoalCollapse:
    """The goal document owns whether its controls are collapsed."""

    collapsed: bool


class GoalView[Controls](CoreEventReceiver, ToadNode):
    """A real conversation retains its existing fields and source binding."""

    agent_binding: AgentConnection
    goal_controls: Controls
    goal_display: GoalDisplay
    goal_execution: GoalExecutionControls
    goal_observation: GoalObservation
    goal_bar: GoalCollapse
    feedback: Flash
    prompt: Widget
    project_path: Path
'''
root_module = 'src/toad/goal_interaction.py'
operations = [
    {'operation': 'create_file', 'file_path': str(ROOT / 'src/toad/goal_view.py'), 'source': view},
    {'operation': 'create_file', 'file_path': str(ROOT / 'src/toad/goal_interactions/__init__.py'), 'source': '"""Goal interaction members discovered by their existing declared family."""\n'},
    patch(root_module, [
        ('from typing import ClassVar, TYPE_CHECKING', 'from typing import ClassVar'),
        ('from toad.agent_binding import AgentConnection\n', 'from toad.agent_binding import AgentConnection\nfrom toad.screens.goal_details import GoalDetails\nfrom toad.screens.goal_edit import GoalEdit\nfrom toad.widgets.goal_text import goal_mention_candidates\n'),
        ('if TYPE_CHECKING:\n    from toad.screens.modal import ModalScreen\n    from toad.widgets.conversation import Conversation', 'from toad.screens.modal import ModalScreen\nfrom toad.goal_view import GoalView'),
        ('class GoalInteraction(DeclaredFamily, affix="Interaction"):', 'class GoalInteraction(\n    DeclaredFamily, affix="Interaction", discovery_package="toad.goal_interactions",\n):'),
        ('def __init__(self, view: Conversation):', 'def __init__(self, view: GoalView[GoalSession]):'),
    ]),
    patch('src/toad/widgets/conversation.py', [
        ('from toad.goal_interaction import GoalSession\n', 'from toad.goal_interaction import GoalSession\nfrom toad.goal_view import GoalView\n'),
        ('class Conversation(\n    TranscriptView,', 'class Conversation(\n    GoalView[GoalSession],\n    TranscriptView,'),
    ]),
    patch('src/toad/widgets/goal_bar.py', [
        ('from toad.goal_interaction import GoalInteraction\n', 'from toad.goal_interaction import GoalInteraction\nfrom toad.goal_view import GoalCollapse\nfrom toad.widgets.prompt import DeliveryControls\n'),
        ('class GoalBar(ComposedWhenShown, SidePadding, VerticalGroup):', 'class GoalBar(GoalCollapse, ComposedWhenShown, SidePadding, VerticalGroup):'),
        ('        from toad.widgets.prompt import DeliveryControls\n\n', ''),
    ]),
    patch(root_module, [('        from toad.screens.goal_details import GoalDetails\n', '')], 'HistoryInteraction.apply'),
    patch(root_module, [('        from toad.screens.goal_edit import GoalEdit\n        from toad.widgets.goal_text import goal_mention_candidates\n', '')], 'EditInteraction.apply'),
    patch(root_module, [('return session.display.can_control and session.execution.can_toggle()', 'return super().enabled(session) and session.execution.can_toggle()')], 'ToggleInteraction.visible'),
]
root_path = str(ROOT / root_module)
root_patches = [op for op in operations if op['operation'] == 'patch_target' and op['file_path'] == root_path]
for child_patch in root_patches[1:]:
    root_patches[0]['replacements'].extend(child_patch['replacements'])
operations = [op for op in operations if op not in root_patches[1:]]
reason = 'Preserve original GoalSession identity, exact agent/source checks, weak modal reference, retirement, callbacks, captured values, async read/write timing and paint/publication behavior. GoalView composes existing event/app capabilities and declares fields read by the original controller; Conversation inherits it and keeps its concrete fields. GoalCollapse is supplied by the original GoalBar reactive descriptor. Do not merge GoalSession into Conversation: the replacement-session test requires distinct old/new controls on the same view and agent. Five original interaction classes retain implementations and declared names, except Toggle visible reuses the same parent enabled answer. Native family discovery loads cases after the root exists so dialogs use normal imports. Movement is dependency repair, not a claimed decrease in implementation pairs. Validate MRO/method owners, cold roots, annotations, source-stashed controls and real UI/native consumers. No whole-program annotation proof is claimed for inherited untouched ToadNode app annotations.'
first = {'recipes': [{'recipe_id': 'p23-goal-view-original-capabilities', 'reason': reason, 'operations': operations, 'architecture_guards': [], 'authority_claims': []}], 'architecture_guards': []}
members = ['HistoryInteraction', 'CollapseInteraction', 'ToggleInteraction', 'EditInteraction', 'ClearInteraction']
second = {'recipes': [{'recipe_id': 'p23-goal-interaction-discovery', 'reason': reason, 'operations': [{'operation': 'relocate_symbols_to_new_module', 'file_path': str(ROOT / root_module), 'symbol_qualnames': members, 'destination_path': str(ROOT / 'src/toad/goal_interactions/controls.py'), 'destination_source': '"""Original goal interaction members with normal dialog dependencies."""\n\nfrom __future__ import annotations\n\nfrom toad.screens.goal_details import GoalDetails\nfrom toad.screens.goal_edit import GoalEdit\nfrom toad.widgets.goal_text import goal_mention_candidates\n'}], 'architecture_guards': [], 'authority_claims': []}], 'architecture_guards': []}
third = {'recipes': [{'recipe_id': 'p23-goal-interaction-no-reexports', 'reason': 'Found member import consumers migrate to the actual declarations. The root retains no aliases and never eagerly imports discovered children.', 'operations': [{'operation': 'remove_import_names', 'file_path': str(ROOT / root_module), 'module_name': '.goal_interactions.controls', 'import_names': members}], 'architecture_guards': [], 'authority_claims': []}], 'architecture_guards': []}
OUT.write_text(json.dumps({'stages': [first, second]}, indent=2) + '\n')
print(OUT)
