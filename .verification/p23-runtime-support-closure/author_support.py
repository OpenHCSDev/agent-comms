"""Stage the existing P23 support closure against a retained integrated source snapshot.

No apply call: only NRA-projected source is written beneath this artifact directory.
"""
from __future__ import annotations
import ast
import csv
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

from nominal_refactor_advisor.codemod import (
    CodemodPlanDocument, CodemodPlanRoot, CodemodPlanSequence, CodemodSourceSnapshot,
    PatchTargetOperation, RefactorRecipe, SourceRewriteTarget,
)
from nominal_refactor_advisor.json_reports import json_report_object
from nominal_refactor_advisor.codemod_preflight import CodemodOperationPreflightError
from nominal_refactor_advisor.codemod_import_scopes import ModuleImportScope
from nominal_refactor_advisor.refactors.source_insertion import CreateFileOperation, InsertAfterTargetOperation, RemoveImportBindingsOperation
from nominal_refactor_advisor.refactors.module_move import PromoteGuardedImportOperation
from nominal_refactor_advisor.refactors.class_base import AddClassBaseOperation, ReplaceClassBaseOperation
from author_callbacks import minimal_replacements

ORIGINAL_ROOT = Path('/home/ts/wt/comms-friction--P17')
OUT = Path(__file__).resolve().parent
ROOT = OUT / 'before'
TARGETS = ('execution/turn_stats', 'execution/turn_watchdog', 'message_notification')


def path(module):
    return str(ROOT / 'src/agent_comms' / (module + '.py'))


def target(module, qualname=''):
    return SourceRewriteTarget(file_path=path(module), qualname=qualname) if qualname else SourceRewriteTarget(file_path=path(module))


def doc(label, operations):
    return CodemodPlanDocument(recipes=(RefactorRecipe(recipe_id=label, operations=tuple(operations)),))


def relocate_new(module, names, destination):
    return CodemodPlanRoot.from_json_value({'recipes': [{
        'recipe_id': 'lower-original-' + names[0].lower(),
        'operations': [{'operation': 'relocate_symbols_to_new_module', 'file_path': path(module),
                        'symbol_qualnames': names, 'destination_path': path(destination)}],
    }]})


def capture():
    # Both Python source and package configuration belong to the retained proof root.
    # A virtual source mapping must not read friction's changing rebase configuration.
    for resource in (OUT/'projected').rglob('*'):
        if resource.is_file() and resource.suffix != '.py' and '__pycache__' not in resource.parts:
            destination=ROOT/resource.relative_to(OUT/'projected')
            destination.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(resource,destination)
    sources = {str(file): file.read_text() for file in ROOT.rglob('*.py')}
    manifest=json.loads((OUT/'source-manifest.json').read_text())
    expected={str(ROOT/Path(name).relative_to(ORIGINAL_ROOT)):digest
              for name,digest in manifest['sources'].items()}
    assert {name:hashlib.sha256(text.encode()).hexdigest() for name,text in sources.items()} == expected
    (OUT/'proof-root.json').write_text(json.dumps({
        'original_root':str(ORIGINAL_ROOT),'proof_root':str(ROOT),
        'resources':{str(file.relative_to(ROOT)):hashlib.sha256(file.read_bytes()).hexdigest()
                     for file in ROOT.rglob('*') if file.is_file() and file.suffix != '.py'},
    },indent=2)+'\n')
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


def guarded_plan(snapshot, root, modules=TARGETS):
    operations=[]
    for module in modules:
        file=str(root/'src/agent_comms'/(module+'.py'))
        parsed=snapshot.parsed_module_for_source_path(file)
        for statement in ModuleImportScope.TYPE_CHECKING.import_statements(parsed.module):
            match statement:
                case ast.ImportFrom(module=dependency, names=names, level=0) if dependency:
                    operations.append(PromoteGuardedImportOperation(
                        target=SourceRewriteTarget(file_path=file),module_name=dependency,
                        import_names=tuple(alias.name for alias in names)))
                case _:
                    raise ValueError(f'Unsupported support import: {ast.dump(statement)}')
    return doc('promote-only-cycle-free-remaining-support-imports',operations)


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
        documents=plan.as_sequence().documents
        assert len(documents)==1
        document=documents[0]
        preflight=document.preflight(current)
        label=f'{len(stages):02d}'
        (OUT/f'preflight-{label}.json').write_text(json.dumps(json_report_object(preflight.report),indent=2)+'\n')
        try:
            result=preflight.simulate()
        except CodemodOperationPreflightError:
            failed=CodemodPlanSequence(documents=tuple(stages)+documents)
            (OUT/'refused.plan.json').write_text(json.dumps(json_report_object(failed),indent=2)+'\n')
            (OUT/'refusal.json').write_text(json.dumps(json_report_object(preflight.report),indent=2)+'\n')
            raise
        (OUT/f'stage-{label}.json').write_text(json.dumps(json_report_object(result),indent=2)+'\n')
        assert result.is_clean
        stages.extend(documents)
        reports.append(result)
        current=result.required_after_snapshot
        print('stage',len(stages),'clean',flush=True)

    run(doc('declare-support-contracts', (
        CreateFileOperation(target=target('execution/stats_callbacks'), source=(OUT/'support_contracts.py.txt').read_text()),
        CreateFileOperation(target=target('messaging/notification_contracts'), source=(OUT/'notification_contracts.py.txt').read_text()),
    )))
    run(relocate_new('backend', ['FinishSignal','NoFinishSignal','EventFinishSignal','NO_FINISH_SIGNAL'], 'execution/finish_signal'))
    def stats(p):
        source=p.source
        source=replace_required(source,(
            ('from agent_comms.execution.turn_phase import ModelWaitPhase','from agent_comms.execution.turn_phase import ModelWaitPhase, TurnPhase'),
            ('session: NativeSession','session: WatchdogSessionRead[StatsProgressRead[TurnPhase], StatsRequest]'),
            ('def wait_for(self, watchdog: ProgressWatchdog, active: TurnWait) -> TurnWait:',
             'def wait_for[Wait](self, watchdog: StatsWatchdogRead[Wait], active: Wait) -> Wait:'),
        ))
        return add_import(source,'from agent_comms.execution.stats_callbacks import StatsWatchdogRead, StatsProgressRead, WatchdogSessionRead')

    run(doc('bind-stats-callbacks',(
        patch(current,'execution/turn_stats',stats),
        patch(current,'backend',lambda p:add_import(p.source,'from agent_comms.execution.stats_callbacks import WatchdogSessionRead')),
        patch(current,'execution/turn_watchdog',lambda p:add_import(replace_required(p.source,(
            ('session: TurnSession','session: WatchdogSessionRead[ProgressWatchdog, StatsRequest]'),
        )),'from agent_comms.execution.stats_callbacks import WatchdogSessionRead, StatsWatchdogRead, StatsProgressRead\nfrom agent_comms.execution.turn_stats import StatsRequest')),
    )))
    run(doc('inherit-stats-callbacks',(
        ReplaceClassBaseOperation(target=target('backend','NativeSession'),base_name='NativeSessionCallbacks',replacement_base_name='WatchdogSessionRead[ProgressWatchdog, StatsRequest]'),
        AddClassBaseOperation(target=target('execution/turn_watchdog','ProgressWatchdog'),base_name='StatsWatchdogRead[TurnWait]'),
    )))
    run(doc('inherit-watchdog-phase-read',(
        AddClassBaseOperation(target=target('execution/turn_watchdog','ProgressWatchdog'),base_name='StatsProgressRead[TurnPhase]'),
    )))

    def notifications(p):
        source=p.source
        source=replace_required(source,(
            ('comms: Comms','comms: NotificationEvidenceSource[CapturedNotificationEvidence]'),
            ('revision: WireRevision','revision: RevisionChanges'),
            ('Sequence[Delivered]','Sequence[MessageDeliveryRead[Message]]'),
            ('log: Bus','log: MessageReferencesRead[Message]'),
            ('earlier: WireRevision','earlier: RevisionChanges'),
            ('Mapping[ThreadLookup, RecipientActivity]','Mapping[ThreadLookup, RecipientActivityRead[AssignmentNotice[DeliveryProgress], AssignmentProgress, DeliveryProgress]]'),
            ('    AssignmentResponse,','    AssignmentProgress,\n    AssignmentResponse,'),
            ('        reads = self.comms.bus.reads\n        return CapturedNotificationEvidence(\n            self.comms.root, reads, reads.read(), self.comms.registry.snapshot())',
             '        return self.comms.notification_evidence()'),
        ))
        return add_import(source,'from agent_comms.messaging.notification_contracts import NotificationEvidenceSource, RevisionChanges, RecipientActivityRead\nfrom agent_comms.message_reads import MessageDeliveryRead, MessageReferencesRead')

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

    run(doc('delete-replaced-concrete-annotation-imports',(
        RemoveImportBindingsOperation(target=target('execution/turn_stats'),bound_names=('NativeSession','ProgressWatchdog','TurnWait')),
        RemoveImportBindingsOperation(target=target('execution/turn_watchdog'),bound_names=('TurnSession',)),
        RemoveImportBindingsOperation(target=target('message_notification'),bound_names=('Comms','WireRevision','Bus','Delivered')),
    )))
    run(doc('bind-original-recipient-activity-read',(
        patch(current,'agent_activity',lambda p:add_import(p.source,'from agent_comms.messaging.notification_contracts import RecipientActivityRead')),
    )))
    run(doc('inherit-original-recipient-activity-read',(
        AddClassBaseOperation(target=target('agent_activity','RecipientActivity'),base_name='RecipientActivityRead[AssignmentNotice[DeliveryProgress], AssignmentProgress, DeliveryProgress]'),
    )))
    run(doc('delete-replaced-recipient-activity-annotation-import',(
        RemoveImportBindingsOperation(target=target('message_notification'),bound_names=('RecipientActivity',)),
    )))
    run(guarded_plan(current,ROOT,('execution/turn_stats','execution/turn_watchdog')))
    body_plan=CodemodPlanSequence(documents=tuple(stages))
    (OUT/'support-body.plan.json').write_text(json.dumps(json_report_object(body_plan),indent=2)+'\n')
    for file,source in current.sources_by_file_path.items():
        destination=OUT/'projected'/Path(file).relative_to(ROOT)
        destination.parent.mkdir(parents=True,exist_ok=True)
        destination.write_text(source)
    graph=subprocess.run(['/home/ts/code/projects/agent-comms/.venv/bin/python','/home/ts/wt/import_cycles.py',str(OUT/'projected/src')],capture_output=True,text=True,timeout=30)
    (OUT/'scc-body.log').write_text(graph.stdout+graph.stderr)
    graph.check_returncode()
    pending=guarded_plan(current,ROOT).preflight(current)
    (OUT/'support-imports.precut.preflight.json').write_text(json.dumps(json_report_object(pending.report),indent=2)+'\n')
    run(guarded_plan(current,ROOT))

    plan=CodemodPlanSequence(documents=tuple(stages))
    (OUT/'support.plan.json').write_text(json.dumps(json_report_object(plan),indent=2)+'\n')
    for file,source in current.sources_by_file_path.items():
        dest=OUT/'projected'/Path(file).relative_to(ROOT); dest.parent.mkdir(parents=True,exist_ok=True);dest.write_text(source)
    print('SOURCE PROJECTION ONLY;',len(stages),'stages;',len(reports),'native results; no apply',flush=True)


if __name__=='__main__':
    main()
