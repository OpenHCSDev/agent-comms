"""Build a pinned, composable callback migration in memory; never apply product edits."""
from __future__ import annotations
import ast
import csv
from difflib import SequenceMatcher
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import textwrap

from nominal_refactor_advisor.codemod import (
    CodemodPlanDocument, CodemodPlanRoot, CodemodPlanSequence, CodemodSourceSnapshot, PatchTargetOperation,
    RefactorRecipe, ReplaceTargetOperation, SourceRewriteTarget, SourceTextReplacement,
)
from nominal_refactor_advisor.refactors.source_insertion import CreateFileOperation, EnsureImportOperation
from nominal_refactor_advisor.refactors.class_base import AddClassBaseOperation, ReplaceClassBaseOperation
from nominal_refactor_advisor.json_reports import json_report_object
from nominal_refactor_advisor.codemod_source_edits import SourceTextGeometry

ROOT = Path('/home/ts/wt/comms-friction--P17')
TEMPLATES = Path(__file__).parent
ROSTER_CONTROL = '--peer-roster-only-control' in sys.argv
OUT = TEMPLATES / 'roster-only-control-v12' if ROSTER_CONTROL else TEMPLATES
OUT.mkdir(parents=True, exist_ok=True)
HEAD = 'b47c7c0ded5005be39bfcf9e129297fabc4f127d'
SELECTED = (
    'backend', 'execution/owned_turn', 'execution/turn_inputs', 'execution/turn_output',
    'prompt_state', 'agent_activity', 'registry/thread_status', 'registry/registry_document',
)

def path(name: str) -> str:
    return str(ROOT / 'src/agent_comms' / (name + '.py'))

def pinned_sources() -> dict[str, str]:
    archive = subprocess.run(['git', '-C', str(ROOT), 'archive', HEAD, 'src', 'tests', 'AGENTS.md', 'pyproject.toml'],
                             check=True, capture_output=True, timeout=60).stdout
    sources = {}
    with tarfile.open(fileobj=io.BytesIO(archive)) as stream:
        for entry in stream.getmembers():
            if not entry.isfile():
                continue
            content = stream.extractfile(entry).read()
            if entry.name.endswith('.py'):
                sources[str(ROOT / entry.name)] = content.decode()
            else:
                destination = OUT / 'projected' / entry.name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
    return sources

def census(snapshot: CodemodSourceSnapshot, label: str) -> None:
    with (OUT / (label + '-classes.csv')).open('w') as stream:
        writer = csv.writer(stream)
        writer.writerow(('file', 'line', 'name', 'container', 'status'))
        for parsed in snapshot.parsed_modules:
            if parsed.file_path not in {path(name) for name in SELECTED}:
                continue
            for index, node in parsed.syntax_index.indexed_nodes_of_type(ast.ClassDef):
                parent = parsed.syntax_index.parent_node(index)
                writer.writerow((parsed.file_path, node.lineno, node.name,
                                 type(parent).__name__, 'direct' if parent is parsed.module else 'OPEN'))

def unguard(parsed, node: ast.If) -> SourceTextReplacement:
    geometry = SourceTextGeometry(parsed.source)
    start, end = geometry.required_node_offsets(node)
    begin = geometry.required_node_offsets(node.body[0])[0]
    # Get original statement text from NRA geometry; only remove the guard indentation.
    lines = parsed.source[begin:end].splitlines()
    body = lines[0] + '\n' + textwrap.dedent('\n'.join(lines[1:])) if len(lines) > 1 else lines[0]
    return SourceTextReplacement(old_source=parsed.source[start:end], new_source=body)

def main() -> None:
    sources = pinned_sources()
    (OUT / 'source-manifest.json').write_text(json.dumps({
        'head': HEAD, 'nra': subprocess.run(['git', '-C', '/home/ts/wt/comms-commander-2--6ec911da2d7a4caa8e50cc32b92062d1--nominalrefactoradvisor', 'rev-parse', 'HEAD'], check=True, capture_output=True, text=True).stdout.strip(),
        'sources': {name: hashlib.sha256(text.encode()).hexdigest() for name, text in sources.items()},
    }, indent=2) + '\n')
    snapshot = CodemodSourceSnapshot.from_source_mapping(sources, analysis_roots=(ROOT / 'src',))
    census(snapshot, 'before')
    print(f'Pinned {len(sources)} source modules; original class census retained', flush=True)
    prefix = CodemodPlanRoot.from_json_value(json.loads(Path('/home/ts/wt/comms-friction--P17/.verification/p23-message-contract-trace/guarded-closure-8.plan.json').read_text()))
    if ROSTER_CONTROL:
        prefix = CodemodPlanSequence(documents=prefix.as_sequence().documents[:1])
    relocation = CodemodPlanRoot.from_json_value({'recipes': [{
        'recipe_id': 'p23-lower-original-recipient-selection',
        'operations': [{'operation': 'relocate_symbols_to_new_module',
                        'file_path': path('registry_provenance'),
                        'symbol_qualnames': ['RecipientSelection'],
                        'destination_path': path('recipient_selection')}],
    }]})
    preceding = CodemodPlanSequence.compose((prefix, relocation))
    preceding_result = preceding.simulate(snapshot)
    assert preceding_result.is_clean
    (OUT / 'prefix-simulation.json').write_text(json.dumps(preceding_result.execution_payload(), indent=2) + '\n')
    native = native_document(preceding_result.final_snapshot)
    suffix = CodemodPlanSequence.compose((relocation, native))
    (OUT / 'callback-suffix.plan.json').write_text(json.dumps(json_report_object(suffix), indent=2) + '\n')
    document = CodemodPlanSequence.compose((prefix, suffix))
    (OUT / 'callback-plan.json').write_text(json.dumps(json_report_object(document), indent=2) + '\n')
    result = document.simulate(snapshot)
    (OUT / 'callback-simulation.json').write_text(json.dumps(result.execution_payload(), indent=2) + '\n')
    (OUT / 'callback.diff').write_text(result.unified_diff(sources))
    assert result.is_clean
    after = result.final_snapshot
    census(after, 'after')
    for name, source in after.sources_by_file_path.items():
        destination = OUT / 'projected' / Path(name).relative_to(ROOT)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(source)
    stack = OUT / 'projected/stack'
    if not stack.exists():
        stack.symlink_to(TEMPLATES.parents[1] / 'stack', target_is_directory=True)
    changed = [name for name, text in after.sources_by_file_path.items() if sources.get(name) != text]
    print(f'Clean native simulation; {len(changed)} projected files; no application', flush=True)

def guard_free(parsed) -> str:
    source = parsed.source
    geometry = SourceTextGeometry(source)
    edits = []
    for _, node in parsed.syntax_index.indexed_nodes_of_type(ast.If):
        if isinstance(node.test, ast.Name) and node.test.id == 'TYPE_CHECKING':
            start, end = geometry.required_node_offsets(node)
            body_start = geometry.required_node_offsets(node.body[0])[0]
            fragment = source[body_start:end]
            lines = fragment.splitlines()
            replacement = lines[0] + ('\n' + textwrap.dedent('\n'.join(lines[1:])) if len(lines) > 1 else '')
            edits.append((start, end, replacement))
    for start, end, replacement in sorted(edits, reverse=True):
        source = source[:start] + replacement + source[end:]
    return source


def minimal_replacements(old: str, new: str) -> tuple[SourceTextReplacement, ...]:
    before, after = old.splitlines(keepends=True), new.splitlines(keepends=True)
    matcher = SequenceMatcher(a=before, b=after, autojunk=False)
    for context in (1, 2, 4, 8, 16):
        replacements = tuple(SourceTextReplacement(
            old_source=''.join(before[group[0][1]:group[-1][2]]),
            new_source=''.join(after[group[0][3]:group[-1][4]]),
        ) for group in matcher.get_grouped_opcodes(context))
        if all(item.old_source and old.count(item.old_source) == 1 for item in replacements):
            return replacements
    raise ValueError('No unique small native patch hunks')


def native_document(snapshot):
    creations = tuple(CreateFileOperation(target=SourceRewriteTarget(file_path=path(name)),
        source=(TEMPLATES / filename).read_text()) for name, filename in (
            ('execution/session_retention', 'session_retention.py.txt'),
            ('execution/native_session_callbacks', 'native_session_callbacks.py.txt'),
            ('execution/owned_turn_runner', 'owned_turn_runner.py.txt'),
            ('target_command_bindings', 'target_command_bindings.py.txt'),
            ('registry/presence_reads', 'presence_reads.py.txt'),
            ('message_send_reads', 'message_send_reads.py.txt'),
            ('delivery_recipients', 'delivery_recipients.py.txt'),
            ('activity_registry_reads', 'activity_registry_reads.py.txt'),
            ('runtime_info_projection', 'runtime_info_projection.py.txt'),
            ('assignment_notice', 'assignment_notice.py.txt'),
        ))
    operations = list(creations)
    # Authored declaration changes; every original implementation body stays in place.
    replacements = {
        'agent_activity': (
            ('from agent_comms.agent_events import RuntimeInfoObservation', ''),
            ('from agent_comms.bus_publication import Delivered', ''),
            ('from agent_comms.message_notification import MessageNotification', ''),
            ('from agent_comms.registry.registration import Registration', ''),
            ('from agent_comms.registry.registry_document import RegistrySnapshot', ''),
            ('MessageNotification', 'AssignmentNotice[DeliveryProgress]'),
            ('registry: Registration', 'registry: ActivityRegistration'),
            ('snapshot: RegistrySnapshot', 'snapshot: ActivityRegistryRead'),
            ('tuple[Delivered, FrozenRecipient]', 'tuple[DeliveryRecipients, FrozenRecipient]'),
            ('RuntimeInfoWrites["RuntimeInfoObservation"]', 'RuntimeInfoWrites[RuntimeInfoProjection[AgentRuntimeInfo]]'),
            ('observation: RuntimeInfoObservation', 'observation: RuntimeInfoProjection[AgentRuntimeInfo]'),
            ('replace(notification, state=TakenProgress(progress))', 'notification.with_state(TakenProgress(progress))'),
            ('replace(notification, state=state)', 'notification.with_state(state)'),
        ),
        'message_notification': (
            ('class MessageNotification:', 'class MessageNotification(AssignmentNotice[DeliveryProgress]):'),
            ('    @property\n    def answer(self)', '    def with_state(self, state: DeliveryProgress) -> MessageNotification:\n        return replace(self, state=state)\n\n    @property\n    def answer(self)'),
        ),
        'notification_progress': (
            ('from agent_comms.message_notification import MessageNotification', ''),
            ('from agent_comms.bus_publication import Delivered', 'from agent_comms.message_send_reads import PublishedMessageRead'),
            ('message: Delivered', 'message: PublishedMessageRead'),
            ('notification: MessageNotification', 'notification: AssignmentNotice[DeliveryProgress]'),
            (') -> MessageNotification', ') -> AssignmentNotice[DeliveryProgress]'),
        ),
        'runtime_info': (
            ('from agent_comms.agent_events import RuntimeInfoObservation', ''),
            ('from agent_comms.registry.threads import Thread', 'from agent_comms.thread_identity_source import ThreadIdentitySource'),
            ('observation: RuntimeInfoObservation', 'observation: RuntimeInfoProjection[AgentRuntimeInfo]'),
            ('threads: Sequence[Thread]', 'threads: Sequence[ThreadIdentitySource]'),
        ),
        'agent_events': (
            ('class RuntimeInfoObservation(AgentEvent):', 'class RuntimeInfoObservation(AgentEvent, RuntimeInfoProjection[AgentRuntimeInfo]):'),
        ),
        'registry/registration': (('class Registration:', 'class Registration(ActivityRegistration):'),),
        'registry/registry_store': (
            ('class RegistryStore(RegistryLocking, RegistryFile, CarriedDocument[RegistryDocument],',
             'class RegistryStore(RegistryLocking, RegistryFile, CarriedDocument[RegistryDocument], RegistryPathRead,'),
        ),
        'registry/thread_status': (
            ('from agent_comms.registry.registry_document import (\n    RegistryEntry,\n    RegistrySnapshot,\n)', ''),
            ('from agent_comms.registry.threads import Thread', ''),
            ('from agent_comms.messaging.sending import Messaging', ''),
            ('messaging: Messaging', 'messaging: PresenceMessageSender'),
            ('snapshot: RegistrySnapshot', 'snapshot: PresenceSnapshot'),
            ('entry: RegistryEntry', 'entry: PresenceEntry'),
            ('thread: Thread', 'thread: PresenceThread'),
            ('tuple[Thread, ...]', 'tuple[PresenceThread, ...]'),
            ('ThreadOwnerStatus["RegistrySnapshot", "Thread"]', 'ThreadOwnerStatus[PresenceSnapshot, PresenceThread]'),
        ),
        'registry/registry_document': (
            ('from agent_comms.bus_publication import Delivered', 'from agent_comms.delivery_recipients import DeliveryRecipients'),
            ('original: Delivered', 'original: DeliveryRecipients'),
            ('from agent_comms.cli_command import CliCommand', ''),
            ('from agent_comms.comms import Comms', ''),
            ('[C: CliCommand]', '[Context, C: TargetCommandBindings[Context, Thread, ThreadStatus, SurroundingChannel, Checkout, RegistrySnapshot, CatalogAccess, ResolvedChannel]]'),
            ('comms: Comms', 'comms: Context'),
            ('class RegistryEntry:', 'class RegistryEntry(PresenceEntry):'),
            ('    ThreadOwnerSource[Thread],\n', '    ThreadOwnerSource[Thread],\n    PresenceSnapshot, ActivityRegistryRead,\n'),
        ),
        'registry/threads': (
            ('class Thread(ThreadProvenance, CatalogSelection, DeclaredFamily, TaskOwner, GoalFailureOwner,',
             'class Thread(ThreadProvenance, CatalogSelection, DeclaredFamily, TaskOwner, GoalFailureOwner, PresenceThread,'),
        ),
        'messaging/messages': (
            ('class Message(MessageInput, TurnOrigin["Message"]):',
             'class Message(MessageInput, TurnOrigin["Message"], MessageReferenceRead):'),
        ),
        'send_result': (('class SendResult:', 'class SendResult(CommittedMessageRead):'),),
        'messaging/sending': (('class Messaging:', 'class Messaging(PresenceMessageSender):'),),
        'bus_publication': (
            ('class Delivered(BusRecord, AudiencedMessage, declared_name=None):',
             'class Delivered(BusRecord, AudiencedMessage, DeliveryRecipients, declared_name=None):'),
        ),
        'cli_command': (
            ('    Generic[Answer],', '    TargetCommandBindings[Comms, "Thread", "ThreadStatus", SurroundingChannel, Checkout, RegistrySnapshot, CatalogAccess, ResolvedChannel],\n    Generic[Answer],'),
        ),
        'execution/owned_turn': (
            ('from agent_comms.execution.turn_runner import TurnRunner',
             'from agent_comms.execution.owned_turn_runner import OwnedTurnRunner'),
            ('runner: TurnRunner', 'runner: OwnedTurnRunner'),
        ),
        'execution/turn_runner': (
            ('class TurnRunner:', 'class TurnRunner(OwnedTurnRunner):'),
        ),
        'backend': (
            ('from agent_comms.pi_native_backend import PersistentPiSession',
             'from agent_comms.execution.session_retention import NativeSessionRetention'),
            ('persistent_session: PersistentPiSession', 'persistent_session: NativeSessionRetention[PiSessionChild, NativeChildState, NativeSessionIdentity]'),
            ('native_session: PersistentPiSession', 'native_session: NativeSessionRetention[PiSessionChild, NativeChildState, NativeSessionIdentity]'),
            ('from agent_comms.execution.native_child import PiSessionChild', 'from agent_comms.execution.native_child import NativeChildState, PiSessionChild'),
            ('from agent_comms.execution.native_session_reopen import PiSessionId', 'from agent_comms.execution.native_session_reopen import NativeSessionIdentity, PiSessionId'),
            ('class NativeSession(MroDispatch, TerminalSessionRead):',
             'class NativeSession(MroDispatch, NativeSessionCallbacks):'),
            ('class TurnSession(NativeSession):',
             'class TurnSession(NativeSession, PromptSessionCallbacks):'),
        ),
        'prompt_state': (
            ('from agent_comms.backend import NativeSession',
             'from agent_comms.execution.native_session_callbacks import NativeSessionCallbacks'),
            ('session: NativeSession', 'session: NativeSessionCallbacks'),
            ('class PromptState(TerminalPromptRead):', 'class PromptState(SessionPromptCallbacks):'),
        ),
        'execution/turn_inputs': (
            ('from agent_comms.backend import NativeSession, TurnSession',
             'from agent_comms.execution.native_session_callbacks import NativeSessionCallbacks, PromptSessionCallbacks'),
            ('session: NativeSession', 'session: NativeSessionCallbacks'),
            ('session: TurnSession', 'session: PromptSessionCallbacks'),
            ('class InputForwarding(MroDispatch, TerminalInputsRead):',
             'class InputForwarding(MroDispatch, SessionInputsCallbacks):'),
        ),
        'execution/turn_output': (
            ('from agent_comms.backend import NativeSession',
             'from agent_comms.execution.native_session_callbacks import NativeSessionCallbacks'),
            ('session: NativeSession', 'session: NativeSessionCallbacks'),
            ('class TurnOutput(TerminalOutputRead):', 'class TurnOutput(SessionOutputCallbacks):'),
        ),
        'pi_native_backend': (
            ('class PersistentPiSession:',
             'class PersistentPiSession(NativeSessionRetention["PiSessionChild", "NativeChildState", NativeSessionIdentity]):'),
        ),
        'execution/turn_stats': (
            ('class StatsRequest:', 'class StatsRequest(SessionStatsCallbacks):'),
        ),
    }
    imports = {
        'agent_activity': 'from agent_comms.activity_registry_reads import ActivityRegistryRead, ActivityRegistration\nfrom agent_comms.delivery_recipients import DeliveryRecipients\nfrom agent_comms.runtime_info_projection import RuntimeInfoProjection\nfrom agent_comms.runtime_info import AgentRuntimeInfo\nfrom agent_comms.assignment_notice import AssignmentNotice',
        'message_notification': 'from agent_comms.assignment_notice import AssignmentNotice',
        'notification_progress': 'from agent_comms.assignment_notice import AssignmentNotice',
        'runtime_info': 'from agent_comms.runtime_info_projection import RuntimeInfoProjection',
        'agent_events': 'from agent_comms.runtime_info_projection import RuntimeInfoProjection',
        'registry/registration': 'from agent_comms.activity_registry_reads import ActivityRegistration',
        'registry/registry_store': 'from agent_comms.activity_registry_reads import RegistryPathRead',
        'registry/thread_status': 'from agent_comms.registry.presence_reads import PresenceThread, PresenceEntry, PresenceSnapshot\nfrom agent_comms.message_send_reads import PresenceMessageSender',
        'registry/registry_document': 'from agent_comms.registry.presence_reads import PresenceEntry, PresenceSnapshot\nfrom agent_comms.activity_registry_reads import ActivityRegistryRead\nfrom agent_comms.target_command_bindings import TargetCommandBindings',
        'registry/threads': 'from agent_comms.registry.presence_reads import PresenceThread',
        'messaging/messages': 'from agent_comms.message_send_reads import MessageReferenceRead',
        'send_result': 'from agent_comms.message_send_reads import CommittedMessageRead',
        'messaging/sending': 'from agent_comms.message_send_reads import PresenceMessageSender',
        'bus_publication': 'from agent_comms.delivery_recipients import DeliveryRecipients\nfrom agent_comms.message_send_reads import PublishedMessageRead',
        'cli_command': 'from agent_comms.target_command_bindings import TargetCommandBindings',
        'execution/owned_turn': 'from agent_comms.execution.owned_turn_runner import OwnedTurnRunner',
        'execution/turn_runner': 'from agent_comms.execution.owned_turn_runner import OwnedTurnRunner',
        'backend': 'from agent_comms.execution.native_session_callbacks import NativeSessionCallbacks, PromptSessionCallbacks',
        'prompt_state': 'from agent_comms.execution.native_session_callbacks import SessionPromptCallbacks',
        'execution/turn_inputs': 'from agent_comms.execution.native_session_callbacks import SessionInputsCallbacks',
        'execution/turn_output': 'from agent_comms.execution.native_session_callbacks import SessionOutputCallbacks',
        'pi_native_backend': 'from agent_comms.execution.session_retention import NativeSessionRetention',
        'execution/turn_stats': 'from agent_comms.execution.native_session_callbacks import SessionStatsCallbacks',
    }
    missing_pairs = []
    for name, pairs in replacements.items():
        parsed = snapshot.parsed_module_for_source_path(path(name))
        source = guard_free(parsed) if name in SELECTED or name in ('cli_command', 'runtime_info', 'notification_progress') else parsed.source
        for old, new in pairs:
            if old.lstrip().startswith('class ') or old in ('    ThreadOwnerSource[Thread],\n', '    Generic[Answer],'):
                continue
            if old not in source:
                missing_pairs.append((name, old))
            else:
                source = source.replace(old, new)
        if name == 'agent_activity':
            for _, node in parsed.syntax_index.indexed_nodes_of_type(ast.ImportFrom):
                if node.col_offset and node.module != 'typing' and node.module == 'agent_comms.diagnostics':
                    start, end = SourceTextGeometry(parsed.source).required_node_offsets(node)
                    original = parsed.source[start - node.col_offset:end]
                    assert original in source
                    source = source.replace(original, '')
                    source = source.replace('from __future__ import annotations\n', 'from __future__ import annotations\n' + textwrap.dedent(original) + '\n', 1)
        if name == 'execution/owned_turn':
            for _, node in parsed.syntax_index.indexed_nodes_of_type(ast.ImportFrom):
                if node.module == 'agent_comms.execution.turn_context' and node.col_offset:
                    start, end = SourceTextGeometry(parsed.source).required_node_offsets(node)
                    original = parsed.source[start - node.col_offset:end]
                    assert original in source
                    source = source.replace(original, '')
                    source = source.replace('from __future__ import annotations\n',
                                            'from __future__ import annotations\n' + textwrap.dedent(original) + '\n', 1)
        # Insert the declared role import after the future import, not inside a guard.
        if 'from __future__ import annotations\n' in source:
            source = source.replace('from __future__ import annotations\n',
                                    'from __future__ import annotations\n\n' + imports[name] + '\n', 1)
        else:
            source = imports[name] + '\n' + source
        operations.append(PatchTargetOperation(
            target=SourceRewriteTarget(file_path=path(name)),
            replacements=minimal_replacements(parsed.source, source),
        ))
    assert not missing_pairs, missing_pairs
    bases = (
        ('registry/registry_document', 'RegistryEntry', 'PresenceEntry'),
        ('registry/registry_document', 'RegistrySnapshot', 'PresenceSnapshot'),
        ('registry/threads', 'Thread', 'PresenceThread'),
        ('messaging/messages', 'Message', 'MessageReferenceRead'),
        ('send_result', 'SendResult', 'CommittedMessageRead'),
        ('messaging/sending', 'Messaging', 'PresenceMessageSender'),
        ('bus_publication', 'Delivered', 'DeliveryRecipients'),
        ('execution/turn_runner', 'TurnRunner', 'OwnedTurnRunner'),
        ('backend', 'TurnSession', 'PromptSessionCallbacks'),
        ('pi_native_backend', 'PersistentPiSession', 'NativeSessionRetention["PiSessionChild", "NativeChildState", NativeSessionIdentity]'),
        ('execution/turn_stats', 'StatsRequest', 'SessionStatsCallbacks'),
        ('message_notification', 'MessageNotification', 'AssignmentNotice[DeliveryProgress]'),
        ('agent_events', 'RuntimeInfoObservation', 'RuntimeInfoProjection[AgentRuntimeInfo]'),
        ('registry/registration', 'Registration', 'ActivityRegistration'),
        ('registry/registry_store', 'RegistryStore', 'RegistryPathRead'),
    )
    base_ops = [AddClassBaseOperation(target=SourceRewriteTarget(file_path=path(module), qualname=qualname), base_name=base)
                for module, qualname, base in bases]
    for module, qualname, original, replacement in (
        ('cli_command', 'CliCommand', 'Generic[Answer]', 'TargetCommandBindings[Comms, Thread, ThreadStatus, SurroundingChannel, Checkout, RegistrySnapshot, CatalogAccess, ResolvedChannel]'),
        ('backend', 'NativeSession', 'TerminalSessionRead', 'NativeSessionCallbacks'),
        ('prompt_state', 'PromptState', 'TerminalPromptRead', 'SessionPromptCallbacks'),
        ('execution/turn_inputs', 'InputForwarding', 'TerminalInputsRead', 'SessionInputsCallbacks'),
        ('execution/turn_output', 'TurnOutput', 'TerminalOutputRead', 'SessionOutputCallbacks'),
    ):
        base_ops.append(ReplaceClassBaseOperation(target=SourceRewriteTarget(file_path=path(module), qualname=qualname), base_name=original, replacement_base_name=replacement))
    return CodemodPlanSequence.compose((CodemodPlanDocument(recipes=(RefactorRecipe(
        recipe_id='p23-native-callback-declarations',
        reason='Explore the admitted native session callback cut on original providers. Keep constructors, fields and executable bodies unchanged; callback receivers use nominal bases rather than importing their implementing backend. This is artifacts-only source simulation with cold/MRO/consumer qualification still required.',
        operations=tuple(operations),
    ),)), CodemodPlanDocument(recipes=(RefactorRecipe(recipe_id='p23-bind-actual-callback-providers', operations=tuple(base_ops)),)),
        CodemodPlanDocument(recipes=(RefactorRecipe(recipe_id='p23-compose-snapshot-activity-read', operations=(AddClassBaseOperation(target=SourceRewriteTarget(file_path=path('bus_publication'), qualname='Delivered'), base_name='PublishedMessageRead'), AddClassBaseOperation(target=SourceRewriteTarget(file_path=path('registry/registry_document'), qualname='RegistrySnapshot'), base_name='ActivityRegistryRead'), AddClassBaseOperation(target=SourceRewriteTarget(file_path=path('cli_command'), qualname='CliCommand'), base_name='Generic[Answer]'))),))))


if __name__ == '__main__':
    main()
