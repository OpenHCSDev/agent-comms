"""Remove the named Registration -> SelectedParticipant bootstrap dependency.

Native source simulation only; friction owns shared application and startup.
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
    CodemodPlanDocument, CodemodPlanSequence, CodemodSourceSnapshot,
    PatchTargetOperation, RefactorRecipe, SourceRewriteTarget, SourceTextReplacement,
)
from nominal_refactor_advisor.json_reports import json_report_object
from nominal_refactor_advisor.refactors.class_base import AddClassBaseOperation
from nominal_refactor_advisor.refactors.source_insertion import (
    CreateFileOperation, EnsureImportOperation, RemoveImportBindingsOperation,
)

OUT = Path(__file__).resolve().parent
ROOT = OUT / 'before'
LIVE = Path('/home/ts/wt/comms-friction--P17')
HEAD = '7b480b21166604751b36e630fdc8a72f1484513a'
CAPABILITY = 'agent_comms.execution.selected.owner_source'
SOURCE = '''"""The mutable original registry owner held by a selected participant."""
from abc import ABC


class RegistryOwnerSource[Owner](ABC):
    __slots__ = ()
    owner: Owner
'''


def target(module, name=None):
    return SourceRewriteTarget(file_path=str(ROOT / 'src/agent_comms' / (module + '.py')), qualname=name)


def document(name, operations):
    return CodemodPlanDocument(recipes=(RefactorRecipe(recipe_id=name, operations=tuple(operations)),))


def main():
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
        'configuration_sha256': hashlib.sha256((ROOT / 'pyproject.toml').read_bytes()).hexdigest(),
        'sources': {path: hashlib.sha256(source.encode()).hexdigest() for path, source in sources.items()},
    }, indent=2) + '\n')
    snapshot = CodemodSourceSnapshot.from_source_mapping(sources, analysis_roots=(ROOT / 'src',))
    stages = (
        document('declare-selected-registry-owner-capability', (
            CreateFileOperation(target=target('execution/selected/owner_source'), source=SOURCE),
            EnsureImportOperation(target=target('execution/selected/participant'),
                                  import_source=f'from {CAPABILITY} import RegistryOwnerSource'),
            EnsureImportOperation(target=target('registry/registration'),
                                  import_source=f'from {CAPABILITY} import RegistryOwnerSource'),
        )),
        document('compose-original-selected-participant-owner-capability', (
            AddClassBaseOperation(target=target('execution/selected/participant', 'SelectedParticipant'),
                                  base_name='RegistryOwnerSource[RegistryOwner]'),
        )),
        document('type-registration-callback-by-its-required-original-owner', (
            PatchTargetOperation(target=target('registry/registration', 'Registration.claim_fresh_session'),
                replacements=(SourceTextReplacement(
                    old_source='participant: selected_participant.SelectedParticipant',
                    new_source='participant: RegistryOwnerSource[RegistryOwner]',
                ),)),
        )),
        document('delete-unread-high-selected-participant-module-import', (
            RemoveImportBindingsOperation(target=target('registry/registration'),
                                          bound_names=('selected_participant',)),
        )),
    )
    for index, stage in enumerate(stages):
        preflight = stage.preflight(snapshot)
        (OUT / f'stage-{index}.preflight.json').write_text(json.dumps(json_report_object(preflight.report), indent=2) + '\n')
        result = preflight.simulate()
        assert result.is_clean
        snapshot = result.required_after_snapshot
        print(f'{index + 1} native-clean stages', flush=True)
    plan = CodemodPlanSequence(documents=stages)
    encoded = json.dumps(json_report_object(plan), indent=2) + '\n'
    (OUT / 'selected-owner.plan.json').write_text(encoded)
    (OUT / 'selected-owner-handoff.plan.json').write_text(encoded.replace(str(ROOT), str(LIVE)))
    for path, source in snapshot.sources_by_file_path.items():
        destination = OUT / 'projected' / Path(path).relative_to(ROOT)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(source)
    print('SOURCE SIMULATION ONLY; original methods, capture/store order and dataclass field positions unchanged', flush=True)


if __name__ == '__main__':
    main()
