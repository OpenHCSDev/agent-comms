"""Move the existing registration callback contract below its consumers."""
from __future__ import annotations
import json
from pathlib import Path
import shutil
from nominal_refactor_advisor.codemod import CodemodPlanDocument, CodemodPlanRoot, CodemodPlanSequence, CodemodSourceSnapshot, PatchTargetOperation, RefactorRecipe, SourceRewriteTarget
from nominal_refactor_advisor.json_reports import json_report_object
from nominal_refactor_advisor.refactors.class_base import ReplaceClassBaseOperation
from nominal_refactor_advisor.refactors.module_move import PromoteGuardedImportOperation
from nominal_refactor_advisor.refactors.source_insertion import EnsureImportOperation
from author_callbacks import minimal_replacements

OUT=Path(__file__).resolve().parent
ROOT=OUT/'namespace-projected'
LIVE=Path('/home/ts/wt/comms-friction--P17')

def target(module,name=None):
    return SourceRewriteTarget(file_path=str(ROOT/'src/agent_comms'/(module+'.py')),qualname=name)

def patch(snapshot,module,changes):
    source=snapshot.parsed_module_for_source_path(target(module).file_path).source
    changed=source
    for before,after in changes:
        assert before in changed,before
        changed=changed.replace(before,after)
    return PatchTargetOperation(target=target(module),replacements=minimal_replacements(source,changed))

def main():
    for source in (OUT/'before').rglob('*'):
        if source.is_file() and source.suffix != '.py' and '__pycache__' not in source.parts:
            destination=ROOT/source.relative_to(OUT/'before')
            destination.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(source,destination)
    snapshot=CodemodSourceSnapshot.from_source_mapping({str(file):file.read_text() for file in ROOT.rglob('*.py')},analysis_roots=(ROOT/'src',))
    stages=[]
    def run(document):
        nonlocal snapshot
        preflight=document.preflight(snapshot)
        (OUT/f'registration-{len(stages)}.preflight.json').write_text(json.dumps(json_report_object(preflight.report),indent=2)+'\n')
        result=preflight.simulate()
        assert result.is_clean
        stages.append(document)
        snapshot=result.required_after_snapshot
        print('registration stage',len(stages),'clean',flush=True)
    def doc(name,operations):
        return CodemodPlanDocument(recipes=(RefactorRecipe(recipe_id=name,operations=tuple(operations)),))
    run(doc('parameterize-original-registration-callback-values',(
        patch(snapshot,'activity_registry_reads',(
            ('class ActivityRegistration(ABC):','class ActivityRegistration[Owner, NativeOwner, Snapshot](ABC):'),
            ('def snapshot(self) -> ActivityRegistryRead:','def snapshot(self) -> Snapshot:'),
            ('def require(self, name: str) -> Thread: ...\n\n    @abstractmethod\n    def lease_local_turn', 'def require(self, name: str) -> Owner: ...\n\n    @abstractmethod\n    def lease_local_turn'),
            (') -> tuple[NativeThread, int]: ...',') -> tuple[NativeOwner, int]: ...'),
        )),
    )))
    move=CodemodPlanRoot.from_json_value({'recipes':[{'recipe_id':'lower-original-registration-read-contract','operations':[{
        'operation':'relocate_symbols_to_new_module','file_path':target('activity_registry_reads').file_path,
        'symbol_qualnames':['RegistryPathRead','ActivityRegistration'],
        'destination_path':target('registry/activity_registration_reads').file_path,
    }]}]})
    run(move.as_sequence().documents[0])
    run(doc('bind-original-providers-to-original-value-types',(
        ReplaceClassBaseOperation(target=target('registry/registration','Registration'),base_name='ActivityRegistration',replacement_base_name='ActivityRegistration[Thread, NativeThread, RegistrySnapshot]'),
        patch(snapshot,'registry/agent_activity',(
            ('from agent_comms.registry.threads import Thread','from agent_comms.registry.threads import NativeThread, Thread'),
            ('registry: ActivityRegistration):','registry: ActivityRegistration[Thread, NativeThread, ActivityRegistryRead]):'),
        )),
    )))
    run(doc('bind-notification-callbacks-to-original-registration-capability',(
        patch(snapshot,'messaging/message_notification',(
            ('from agent_comms.registry.registration import Registration','from agent_comms.registry.activity_registration_reads import ActivityRegistration'),
            ('def references(','def references[Owner: ThreadIdentitySource, NativeOwner: ThreadIdentitySource]('),
            ('def delivery_window(','def delivery_window[Owner: ThreadIdentitySource, NativeOwner: ThreadIdentitySource]('),
            ('def sent_sources(','def sent_sources[Owner: ThreadIdentitySource, NativeOwner: ThreadIdentitySource]('),
            ('def for_sources(','def for_sources[Owner: ThreadIdentitySource, NativeOwner: ThreadIdentitySource]('),
            ('registry: Registration','registry: ActivityRegistration[Owner, NativeOwner, RegistrySnapshot]'),
        )),
    )))
    run(doc('promote-proved-registration-read-import',(
        PromoteGuardedImportOperation(target=target('messaging/message_notification'),module_name='agent_comms.registry.activity_registration_reads',import_names=('ActivityRegistration',)),
    )))
    encoded=json.dumps(json_report_object(CodemodPlanSequence(documents=tuple(stages))),indent=2)+'\n'
    (OUT/'registration-suffix.plan.json').write_text(encoded)
    (OUT/'registration-handoff.plan.json').write_text(encoded.replace(str(ROOT),str(LIVE)))
    for file,source in snapshot.sources_by_file_path.items():
        destination=OUT/'registration-projected'/Path(file).relative_to(ROOT)
        destination.parent.mkdir(parents=True,exist_ok=True)
        destination.write_text(source)
    print('SOURCE SIMULATION ONLY; original fields, procedures, lease ordering retained; no apply',flush=True)

if __name__=='__main__':
    main()
