"""Stage the existing P23 support closure against a retained integrated source snapshot.

No apply call: only NRA-projected source is written beneath this artifact directory.
"""
from __future__ import annotations
import ast
import csv
import hashlib
import json
from pathlib import Path
import subprocess

from nominal_refactor_advisor.codemod import (
    CodemodPlanDocument, CodemodPlanRoot, CodemodPlanSequence, CodemodSourceSnapshot,
    PatchTargetOperation, RefactorRecipe, SourceRewriteTarget,
)
from nominal_refactor_advisor.json_reports import json_report_object
from nominal_refactor_advisor.refactors.source_insertion import CreateFileOperation, InsertAfterTargetOperation
from nominal_refactor_advisor.refactors.class_base import AddClassBaseOperation, ReplaceClassBaseOperation
from nominal_refactor_advisor.refactors.class_member_authority import PromoteClassMembersToAncestorOperation
from author_callbacks import guard_free, minimal_replacements

ROOT = Path('/home/ts/wt/comms-friction--P17')
OUT = Path(__file__).parent
TARGETS = ('execution/turn_stats', 'pi_native_backend', 'message_notification')


def path(module):
    return str(ROOT / 'src/agent_comms' / (module + '.py'))


def target(module, qualname=''):
    return SourceRewriteTarget(file_path=path(module), qualname=qualname) if qualname else SourceRewriteTarget(file_path=path(module))


def doc(label, operations):
    return CodemodPlanDocument(recipes=(RefactorRecipe(recipe_id=label, operations=tuple(operations)),))


def relocate(module, names, destination):
    return CodemodPlanRoot.from_json_value({'recipes': [{
        'recipe_id': 'lower-original-' + names[0].lower(),
        'operations': [{'operation': 'relocate_symbols_to_module', 'file_path': path(module),
                        'symbol_qualnames': names, 'destination_path': path(destination)}],
    }]})


def relocate_new(module, names, destination):
    return CodemodPlanRoot.from_json_value({'recipes': [{
        'recipe_id': 'lower-original-' + names[0].lower(),
        'operations': [{'operation': 'relocate_symbols_to_new_module', 'file_path': path(module),
                        'symbol_qualnames': names, 'destination_path': path(destination)}],
    }]})


def capture():
    retained = OUT / 'before'
    if retained.is_dir():
        sources = {str(ROOT / file.relative_to(retained)): file.read_text()
                   for file in retained.rglob('*.py')}
        expected = json.loads((OUT/'source-manifest.json').read_text())['sources']
        assert {name: hashlib.sha256(text.encode()).hexdigest() for name,text in sources.items()} == expected
        return sources
    listing = subprocess.run(['git', '-C', str(ROOT), 'ls-files', '--cached', '--others',
                              '--exclude-standard', 'src', 'tests', 'AGENTS.md', 'pyproject.toml'], check=True,
                             capture_output=True, text=True, timeout=30).stdout.splitlines()
    sources = {}
    resources = {}
    for rel in sorted(set(listing)):
        file = ROOT / rel
        if not file.is_file():
            continue
        content = file.read_bytes()
        if rel.endswith('.py'):
            sources[str(file)] = content.decode()
        else:
            resources[rel] = content
    manifest = {'head': subprocess.run(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'],
                     check=True, capture_output=True, text=True).stdout.strip(),
                'sources': {name: hashlib.sha256(source.encode()).hexdigest()
                            for name, source in sources.items()}}
    (OUT / 'source-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    for file, source in sources.items():
        dest = OUT / 'before' / Path(file).relative_to(ROOT)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(source)
    for rel, content in resources.items():
        dest = OUT / 'projected' / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
    return sources


def patch(snapshot, module, transform):
    original = snapshot.parsed_module_for_source_path(path(module))
    new = transform(original)
    return PatchTargetOperation(target=target(module), replacements=minimal_replacements(original.source, new))


def add_import(source, text):
    future = 'from __future__ import annotations\n'
    if future in source:
        return source.replace(future, future + '\n' + text + '\n', 1)
    return text + '\n' + source


def replace_required(source, changes):
    missing = []
    for old, new in changes:
        if old not in source:
            missing.append(old)
        else:
            source = source.replace(old, new)
    assert not missing, missing
    return source


def main():
    sources = capture()
    original = current = CodemodSourceSnapshot.from_source_mapping(sources, analysis_roots=(ROOT/'src',))
    with (OUT/'before-classes.csv').open('w') as stream:
        writer=csv.writer(stream); writer.writerow(('file','line','class','container'))
        for module in TARGETS:
            parsed=current.parsed_module_for_source_path(path(module))
            for index,node in parsed.syntax_index.indexed_nodes_of_type(ast.ClassDef):
                writer.writerow((parsed.file_path,node.lineno,node.name,type(parsed.syntax_index.parent_node(index)).__name__))
    stages=[]; reports=[]

    def run(plan):
        nonlocal current
        result=plan.as_sequence().simulate(current)
        label=f'{len(stages):02d}'
        (OUT/f'stage-{label}.json').write_text(json.dumps(result.execution_payload(),indent=2)+'\n')
        assert result.is_clean
        stages.extend(plan.as_sequence().documents)
        reports.append(result)
        current=result.final_snapshot
        print('stage',len(stages),'clean',flush=True)

    run(doc('declare-support-contracts', (
        CreateFileOperation(target=target('execution/stats_callbacks'), source=(OUT/'support_contracts.py.txt').read_text()),
        CreateFileOperation(target=target('messaging/notification_contracts'), source=(OUT/'notification_contracts.py.txt').read_text()),
        CreateFileOperation(target=target('execution/event_vocabulary'), source='"""Runtime event vocabularies and their original declaration check."""\nfrom __future__ import annotations\nfrom typing import ClassVar\n'),
    )))
    run(relocate_new('backend', ['FinishSignal','NoFinishSignal','EventFinishSignal','NO_FINISH_SIGNAL'], 'execution/finish_signal'))
    run(relocate('agent_backend', ['BackendDeclarationError','_event_family_problems'], 'execution/event_vocabulary'))
    run(relocate('pi_native_backend', ['PI_0_85_1_EVENT_KINDS'], 'execution/event_vocabulary'))
    run(doc('declare-vocabulary-providers', (patch(current, 'execution/event_vocabulary', lambda parsed:
        parsed.source + '\n\nclass RuntimeEventVocabulary:\n    """The runtime declaration checks its complete external event vocabulary."""\n\n\nclass PiNativeEventVocabulary(RuntimeEventVocabulary):\n    """Pi owns the pinned native event vocabulary."""\n'),)))
    run(doc('bind-vocabulary-ancestry', (
        patch(current,'agent_backend',lambda p:add_import(p.source,'from agent_comms.execution.event_vocabulary import RuntimeEventVocabulary')),
        patch(current,'pi_native_backend',lambda p:add_import(p.source,'from agent_comms.execution.event_vocabulary import PiNativeEventVocabulary')),
    )))
    run(doc('inherit-vocabulary-check', (
        AddClassBaseOperation(target=target('agent_backend','AgentBackend'),base_name='RuntimeEventVocabulary'),
        AddClassBaseOperation(target=target('pi_native_backend','PiNativeBackend'),base_name='PiNativeEventVocabulary'),
    )))
    run(doc('move-original-vocabulary-check', (
        PromoteClassMembersToAncestorOperation(target=target('agent_backend','AgentBackend'),destination=target('execution/event_vocabulary','RuntimeEventVocabulary'),member_names=('require_event_family','event_kinds')),
        PromoteClassMembersToAncestorOperation(target=target('pi_native_backend','PiNativeBackend'),destination=target('execution/event_vocabulary','PiNativeEventVocabulary'),member_names=('event_kinds',)),
    )))
    run(doc('validate-vocabulary-without-loading-implementation', (patch(current,'pi_events',lambda p:replace_required(p.source,(
        ('from agent_comms.pi_native_backend import PiNativeBackend  # noqa: E402','from agent_comms.execution.event_vocabulary import PiNativeEventVocabulary  # noqa: E402'),
        ('PiNativeBackend.require_event_family(NativePiEvent)','PiNativeEventVocabulary.require_event_family(NativePiEvent)'),
    ))),)))

    def stats(p):
        source=guard_free(p)
        source=replace_required(source,(
            ('from agent_comms.backend import NativeSession',''),
            ('from agent_comms.execution.turn_watchdog import ProgressWatchdog, TurnWait',''),
            ('from agent_comms.execution.turn_phase import ModelWaitPhase','from agent_comms.execution.turn_phase import ModelWaitPhase, TurnPhase'),
            ('session: NativeSession','session: WatchdogSessionRead[StatsProgressRead[TurnPhase]]'),
            ('def wait_for(self, watchdog: ProgressWatchdog, active: TurnWait) -> TurnWait:',
             'def wait_for[Wait](self, watchdog: StatsWatchdogRead[Wait], active: Wait) -> Wait:'),
        ))
        return add_import(source,'from agent_comms.execution.stats_callbacks import StatsWatchdogRead, StatsProgressRead, WatchdogSessionRead')

    run(doc('bind-stats-callbacks',(
        patch(current,'execution/turn_stats',stats),
        patch(current,'backend',lambda p:add_import(p.source,'from agent_comms.execution.stats_callbacks import WatchdogSessionRead')),
        patch(current,'execution/turn_watchdog',lambda p:add_import(replace_required(guard_free(p),(
            ('from agent_comms.backend import TurnSession',''),
            ('session: TurnSession','session: WatchdogSessionRead[ProgressWatchdog]'),
        )),'from agent_comms.execution.stats_callbacks import WatchdogSessionRead, StatsWatchdogRead, StatsProgressRead')),
    )))
    run(doc('inherit-stats-callbacks',(
        ReplaceClassBaseOperation(target=target('backend','NativeSession'),base_name='NativeSessionCallbacks',replacement_base_name='WatchdogSessionRead[ProgressWatchdog]'),
        AddClassBaseOperation(target=target('execution/turn_watchdog','ProgressWatchdog'),base_name='StatsWatchdogRead[TurnWait]'),
    )))
    run(doc('inherit-watchdog-phase-read',(
        AddClassBaseOperation(target=target('execution/turn_watchdog','ProgressWatchdog'),base_name='StatsProgressRead[TurnPhase]'),
    )))

    def notifications(p):
        source=guard_free(p)
        source=replace_required(source,(
            ('from agent_comms.comms import Comms',''),
            ('from agent_comms.presentation import WireRevision',''),
            ('from agent_comms.bus import Bus',''),
            ('from agent_comms.bus_publication import Delivered',''),
            ('comms: Comms','comms: NotificationEvidenceSource[CapturedNotificationEvidence]'),
            ('revision: WireRevision','revision: RevisionChanges'),
            ('Sequence[Delivered]','Sequence[MessageDeliveryRead[Message]]'),
            ('log: Bus','log: MessageReferencesRead[Message]'),
            ('earlier: WireRevision','earlier: RevisionChanges'),
            ('        reads = self.comms.bus.reads\n        return CapturedNotificationEvidence(\n            self.comms.root, reads, reads.read(), self.comms.registry.snapshot())',
             '        return self.comms.notification_evidence()'),
        ))
        return add_import(source,'from agent_comms.messaging.notification_contracts import NotificationEvidenceSource, RevisionChanges\nfrom agent_comms.message_reads import MessageDeliveryRead, MessageReferencesRead')

    run(doc('bind-notification-callbacks',(
        patch(current,'message_notification',notifications),
        patch(current,'presentation',lambda p:add_import(p.source,'from agent_comms.messaging.notification_contracts import RevisionChanges')),
        patch(current,'comms',lambda p:add_import(p.source,'from agent_comms.messaging.notification_contracts import NotificationEvidenceSource\nfrom agent_comms.message_notification import CapturedNotificationEvidence')),
    )))
    run(doc('own-notification-evidence-acquisition',(
        InsertAfterTargetOperation(target=target('comms','Comms.__init__'),source='''    def notification_evidence(self) -> CapturedNotificationEvidence:
        reads = self.bus.reads
        return CapturedNotificationEvidence(
            self.root, reads, reads.read(), self.registry.snapshot())'''),
    )))
    run(doc('inherit-notification-callbacks',(
        AddClassBaseOperation(target=target('presentation','ReadState'),base_name='RevisionChanges'),
        AddClassBaseOperation(target=target('comms','Comms'),base_name='NotificationEvidenceSource[CapturedNotificationEvidence]'),
    )))

    plan=CodemodPlanSequence(documents=tuple(stages))
    (OUT/'support.plan.json').write_text(json.dumps(json_report_object(plan),indent=2)+'\n')
    for file,source in current.sources_by_file_path.items():
        dest=OUT/'projected'/Path(file).relative_to(ROOT); dest.parent.mkdir(parents=True,exist_ok=True);dest.write_text(source)
    print('SOURCE PROJECTION ONLY;',len(stages),'stages;',len(reports),'native results; no apply',flush=True)


if __name__=='__main__':
    main()
