"""Lower the original registry namespace callback contract after the support prefix."""
from __future__ import annotations
import json
from pathlib import Path
from nominal_refactor_advisor.codemod import CodemodPlanDocument, CodemodPlanSequence, CodemodSourceSnapshot, PatchTargetOperation, RefactorRecipe, SourceRewriteTarget
from nominal_refactor_advisor.json_reports import json_report_object
from nominal_refactor_advisor.refactors.source_insertion import CreateFileOperation, EnsureImportOperation
from nominal_refactor_advisor.refactors.class_base import ReplaceClassBaseOperation
from nominal_refactor_advisor.refactors.module_move import PromoteGuardedImportOperation
from author_callbacks import minimal_replacements

OUT=Path(__file__).resolve().parent
ROOT=OUT/'projected'
LIVE=Path('/home/ts/wt/comms-friction--P17')

def target(module, name=None):
    return SourceRewriteTarget(file_path=str(ROOT/'src/agent_comms'/(module+'.py')),qualname=name)

def patch(snapshot,module,changes):
    source=snapshot.parsed_module_for_source_path(target(module).file_path).source
    changed=source
    for before,after in changes:
        assert before in changed,before
        changed=changed.replace(before,after)
    return PatchTargetOperation(target=target(module),replacements=minimal_replacements(source,changed))

def main():
    sources={str(file):file.read_text() for file in ROOT.rglob('*.py')}
    snapshot=CodemodSourceSnapshot.from_source_mapping(sources,analysis_roots=(ROOT/'src',))
    stages=[]
    def run(name,operations):
        nonlocal snapshot
        document=CodemodPlanDocument(recipes=(RefactorRecipe(recipe_id=name,operations=tuple(operations)),))
        preflight=document.preflight(snapshot)
        (OUT/f'namespace-{len(stages)}.preflight.json').write_text(json.dumps(json_report_object(preflight.report),indent=2)+'\n')
        result=preflight.simulate()
        assert result.is_clean
        stages.append(document)
        snapshot=result.required_after_snapshot
        print(name,'clean',flush=True)
    run('declare-namespace-read-contract',(
        CreateFileOperation(target=target('registry/namespace_reads'),source=(OUT/'namespace_reads.py.txt').read_text()),
    ))
    run('bind-original-namespace-provider-and-consumer',(
        EnsureImportOperation(target=target('registry/registry_provenance'),import_source='from agent_comms.registry.namespace_reads import NamespaceRead'),
        EnsureImportOperation(target=target('messaging/message_notification'),import_source='from agent_comms.thread_identity_source import ThreadIdentitySource'),
        patch(snapshot,'messaging/message_notification',(
            ('from agent_comms.registry.registry_provenance import RegistryProvenance','from agent_comms.registry.namespace_reads import NamespaceRead'),
            ('def recorded_delivery_window(','def recorded_delivery_window[Member: ThreadIdentitySource, Selection]('),
            ('def project_delivery_window(','def project_delivery_window[Member: ThreadIdentitySource, Selection]('),
            ('namespace: RegistryProvenance','namespace: NamespaceRead[Member, Selection]'),
            ('snapshot: RegistryProvenance','snapshot: NamespaceRead[Member, Selection]'),
        )),
    ))
    run('inherit-namespace-reads-with-original-concrete-fields',(
        ReplaceClassBaseOperation(target=target('registry/registry_provenance','RegistryProvenance'),base_name='RegistryNames[ThreadProvenance]',replacement_base_name='NamespaceRead[ThreadProvenance, RecipientSelection]'),
    ))
    run('promote-proved-namespace-read-import',(
        PromoteGuardedImportOperation(target=target('messaging/message_notification'),module_name='agent_comms.registry.namespace_reads',import_names=('NamespaceRead',)),
    ))
    sequence=CodemodPlanSequence(documents=tuple(stages))
    encoded=json.dumps(json_report_object(sequence),indent=2)+'\n'
    (OUT/'namespace-suffix.plan.json').write_text(encoded)
    (OUT/'namespace-handoff.plan.json').write_text(encoded.replace(str(ROOT),str(LIVE)))
    for file,source in snapshot.sources_by_file_path.items():
        destination=OUT/'namespace-projected'/Path(file).relative_to(ROOT)
        destination.parent.mkdir(parents=True,exist_ok=True)
        destination.write_text(source)
    print('SOURCE SIMULATION ONLY; original fields/method bodies retained; no apply',flush=True)

if __name__=='__main__':
    main()
