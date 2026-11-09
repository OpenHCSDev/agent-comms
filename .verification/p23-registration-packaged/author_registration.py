"""Lower the original registration contract on the applied package checkpoint.

Native source simulation only. Friction owns application and combined qualification.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile

from nominal_refactor_advisor.codemod import (
    CodemodPlanDocument, CodemodPlanRoot, CodemodPlanSequence,
    CodemodSourceSnapshot, PatchTargetOperation, RefactorRecipe, SourceRewriteTarget,
)
from nominal_refactor_advisor.json_reports import json_report_object
from nominal_refactor_advisor.refactors.class_base import ReplaceClassBaseOperation
from nominal_refactor_advisor.refactors.module_move import PromoteGuardedImportOperation
from nominal_refactor_advisor.refactors.source_insertion import (
    EnsureImportOperation, RemoveImportBindingsOperation,
)
from author_callbacks import minimal_replacements

OUT = Path(__file__).resolve().parent
ROOT = OUT / 'before'
LIVE = Path('/home/ts/wt/comms-friction--P17')
HEAD = '356d83cc039ce74469a30a2817c8275fe90bf527'
ORIGINAL = 'registry/activity_registry_reads'
LOWER = 'registry/registration_reads'
VALUES = 'Thread, NativeThread, RegistrySnapshot, StorageRule, Routing, TurnLeaseFence, TurnCompletion'
ACTIVITY_VALUES = VALUES.replace('RegistrySnapshot', 'ActivityRegistryRead')


def target(module, name=None):
    return SourceRewriteTarget(file_path=str(ROOT / 'src/agent_comms' / (module + '.py')), qualname=name)


def document(name, operations):
    return CodemodPlanDocument(recipes=(RefactorRecipe(recipe_id=name, operations=tuple(operations)),))


def patch(snapshot, module, changes):
    source = snapshot.parsed_module_for_source_path(target(module).file_path).source
    changed = source
    for before, after in changes:
        assert before in changed, before
        changed = changed.replace(before, after)
    return PatchTargetOperation(target=target(module), replacements=minimal_replacements(source, changed))


def capture():
    archive = subprocess.run(
        ['git', '-C', str(LIVE), 'archive', HEAD, 'src', 'tests', 'tools', 'AGENTS.md', 'pyproject.toml'],
        check=True, capture_output=True, timeout=60,
    ).stdout
    (OUT / 'before-source.tar.gz').write_bytes(gzip.compress(archive, mtime=0))
    sources = {}
    with tarfile.open(fileobj=io.BytesIO(archive)) as stream:
        for member in stream.getmembers():
            if not member.isfile():
                continue
            content = stream.extractfile(member).read()
            path = ROOT / member.name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            if path.suffix == '.py':
                sources[str(path)] = content.decode()
            else:
                projected = OUT / 'projected' / member.name
                projected.parent.mkdir(parents=True, exist_ok=True)
                projected.write_bytes(content)
    (OUT / 'source-manifest.json').write_text(json.dumps({
        'head': HEAD,
        'source_root': str(ROOT),
        'sources': {path: hashlib.sha256(source.encode()).hexdigest() for path, source in sources.items()},
        'configuration_sha256': hashlib.sha256((ROOT / 'pyproject.toml').read_bytes()).hexdigest(),
    }, indent=2) + '\n')
    return CodemodSourceSnapshot.from_source_mapping(sources, analysis_roots=(ROOT / 'src',))


def main():
    snapshot = capture()
    before = snapshot.sources_by_file_path
    stages = []

    def run(plan):
        nonlocal snapshot
        preflight = plan.preflight(snapshot)
        (OUT / f'stage-{len(stages)}.preflight.json').write_text(json.dumps(json_report_object(preflight.report), indent=2) + '\n')
        result = preflight.simulate()
        assert result.is_clean
        stages.append(plan)
        snapshot = result.required_after_snapshot
        print(f'{len(stages)} native-clean stages', flush=True)

    run(document('parameterize-original-registration-contract-values', (
        patch(snapshot, ORIGINAL, (
            ('class ActivityRegistration(ABC):',
             'class ActivityRegistration[Owner, NativeOwner, Snapshot, Storage, Route, Lease, Completion](ABC):'),
            ('storage_rule: StorageRule', 'storage_rule: Storage'),
            ('def snapshot(self) -> ActivityRegistryRead:', 'def snapshot(self) -> Snapshot:'),
            ('def require(self, name: str) -> Thread: ...\n\n    @abstractmethod\n    def lease_local_turn',
             'def require(self, name: str) -> Owner: ...\n\n    @abstractmethod\n    def lease_local_turn'),
            ('routing: Routing = NoTurnRouting(),', 'routing: Route = ...,'),
            (') -> tuple[NativeThread, int]: ...', ') -> tuple[NativeOwner, int]: ...'),
            ('def release_turn(self, lease: TurnLeaseFence) -> tuple[bool, TurnCompletion]: ...',
             'def release_turn(self, lease: Lease) -> tuple[bool, Completion]: ...'),
        )),
    )))
    run(document('remove-high-imports-no-longer-read-by-original-contract', (
        RemoveImportBindingsOperation(target=target(ORIGINAL), bound_names=(
            'TurnCompletion', 'TurnLeaseFence', 'NoTurnRouting', 'Routing', 'StorageRule', 'NativeThread',
        )),
    )))
    move = CodemodPlanRoot.from_json_value({'recipes': [{
        'recipe_id': 'move-original-registration-contract-below-its-callbacks',
        'operations': [{
            'operation': 'relocate_symbols_to_new_module',
            'file_path': target(ORIGINAL).file_path,
            'symbol_qualnames': ['RegistryPathRead', 'ActivityRegistration'],
            'destination_path': target(LOWER).file_path,
        }],
    }]})
    run(move.as_sequence().documents[0])
    run(document('bind-original-provider-and-activity-to-original-value-types', (
        ReplaceClassBaseOperation(target=target('registry/registration', 'Registration'),
                                  base_name='ActivityRegistration',
                                  replacement_base_name=f'ActivityRegistration[{VALUES}]'),
        EnsureImportOperation(target=target('registry/registration'),
                              import_source='from agent_comms.execution.turn_lease import TurnLeaseFence, TurnCompletion'),
        EnsureImportOperation(target=target('registry/agent_activity'),
                              import_source='from agent_comms.registry.threads import NativeThread'),
        EnsureImportOperation(target=target('registry/agent_activity'),
                              import_source='from agent_comms.projects.storage_rules import StorageRule'),
        patch(snapshot, 'registry/agent_activity', (
            ('registry: ActivityRegistration):', f'registry: ActivityRegistration[{ACTIVITY_VALUES}]):'),
        )),
    )))
    run(document('use-original-registration-capability-in-notification-callbacks', (
        patch(snapshot, 'messaging/message_notification', (
            ('from agent_comms.registry.registration import Registration',
             'from agent_comms.registry.registration_reads import ActivityRegistration'),
            ('registry: Registration', f'registry: ActivityRegistration[{VALUES}]'),
        )),
        EnsureImportOperation(target=target('messaging/message_notification'),
                              import_source='from agent_comms.registry.threads import Thread, NativeThread'),
        EnsureImportOperation(target=target('messaging/message_notification'),
                              import_source='from agent_comms.projects.storage_rules import StorageRule'),
        EnsureImportOperation(target=target('messaging/message_notification'),
                              import_source='from agent_comms.messaging.routing import Routing'),
        EnsureImportOperation(target=target('messaging/message_notification'),
                              import_source='from agent_comms.execution.turn_lease import TurnLeaseFence, TurnCompletion'),
    )))
    run(document('promote-cycle-proved-registration-callback-import', (
        PromoteGuardedImportOperation(target=target('messaging/message_notification'),
                                      module_name='agent_comms.registry.registration_reads',
                                      import_names=('ActivityRegistration',)),
    )))
    run(document('delete-unused-and-already-runtime-activity-guard-copies', (
        patch(snapshot, 'registry/agent_activity', (
            ('from typing import TYPE_CHECKING\n', ''),
            ('if TYPE_CHECKING:\n'
             '    from agent_comms.agent_backend import AgentBackend\n'
             '    from agent_comms.agent_events import RuntimeInfoObservation\n'
             '    from agent_comms.messaging.bus_publication import Delivered\n'
             '    from agent_comms.messaging.message_notification import MessageNotification\n'
             '    from agent_comms.messaging.notification_progress import AssignmentProgress, DeliveryProgress\n'
             '    from agent_comms.registry.registration import Registration\n'
             '    from agent_comms.registry.registry_document import RegistrySnapshot\n'
             '    from agent_comms.registry.thread_status import ThreadStatus\n', ''),
        )),
    )))
    sequence = CodemodPlanSequence(documents=tuple(stages))
    encoded = json.dumps(json_report_object(sequence), indent=2) + '\n'
    (OUT / 'registration.plan.json').write_text(encoded)
    (OUT / 'registration-handoff.plan.json').write_text(encoded.replace(str(ROOT), str(LIVE)))
    changed = {}
    for path, source in snapshot.sources_by_file_path.items():
        destination = OUT / 'projected' / Path(path).relative_to(ROOT)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(source)
        if before.get(path) != source:
            changed[str(Path(path).relative_to(ROOT))] = hashlib.sha256(source.encode()).hexdigest()
    (OUT / 'changed.json').write_text(json.dumps(changed, indent=2) + '\n')
    print('SOURCE SIMULATION ONLY; actual fields, constructors, defaults and lease procedures unchanged', flush=True)


if __name__ == '__main__':
    main()
