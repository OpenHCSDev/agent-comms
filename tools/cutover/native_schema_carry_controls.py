"""Installed stopped-copy carry controls using actual original Native5 stores.

The release owner supplies a stopped, privately retained original root. This
control neither seeds history/proofs nor stops, launches, retries or prompts an
owner. Running-source journal inventory can exercise request conversion only;
that mode explicitly does not qualify the public stopped-custody installation.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess

from agent_comms.field_codec import FieldCodec
from agent_comms.private_path import PrivateDirectoryRole
from native_schema_carry import (
    NativeSchemaDeclaration, RuntimeNativeFiles, capture_requests,
    carry_compaction, inventory, prepare, row_digest,
)
from publish_openhcs_recovery import digest
from retained_summary_reset import RuntimeCompactionFiles
from routing_recovery import write_original
from runtime_installation import CarryNativeRuntimeInstallation, RuntimeInstallation


def original_declaration(source_python):
    packet = subprocess.run(
        [str(source_python), str(Path(__file__).with_name('native_schema_carry.py')),
         '--declaration'], text=True, capture_output=True, timeout=30, check=True)
    return FieldCodec.decode(NativeSchemaDeclaration, json.loads(packet.stdout))


def journal_observation(path):
    with closing(sqlite3.connect(path.absolute().as_uri()+'?mode=ro', uri=True)) as db:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        try:
            return inventory(db)
        finally:
            db.execute('ROLLBACK')


def require_journal_preserved(before, after):
    from agent_comms.compaction_records import SelectedSummaryAttempt

    name = SelectedSummaryAttempt.declared_name
    unchanged = {key:values for key,values in before.items() if key != name}
    if any(after[key] != values for key,values in unchanged.items()):
        raise AssertionError('Original operation/publication/enrollment/UNKNOWN rows changed')
    current = sorted((operation, session, source, state)
                     for operation,session,source,request,state in after[name])
    if current != sorted(before[name]):
        raise AssertionError('Original selected attempt source/disposition changed')
    return {'rows':{key:len(values) for key,values in before.items()},
            'unchanged_tables_sha256':row_digest(unchanged),
            'original_selected_rows_sha256':row_digest(before[name])}


def run_journal_inventory(base, source_python, inventory_path):
    """Actual running-source backup, privately transformed; no stopped claim."""
    base.mkdir(mode=0o700)
    original = original_declaration(source_python)
    target = NativeSchemaDeclaration.observe()
    if original.release_versions != (9,3,3,5) or target.release_versions != (9,3,3,6):
        raise ValueError('Matched original Native5 and current Native6 declarations required')
    before_sha = digest(inventory_path)
    candidate = base/'compaction-commits.sqlite3'
    shutil.copyfile(inventory_path, candidate)
    candidate.chmod(0o600)
    requests = capture_requests(candidate, source_python, original)
    before = journal_observation(candidate)
    with closing(sqlite3.connect(candidate)) as db:
        db.execute('PRAGMA foreign_keys=OFF')
        db.execute('PRAGMA synchronous=FULL')
        db.execute('BEGIN IMMEDIATE')
        try:
            evidence = carry_compaction(db, original, target, requests)
            db.commit()
        except BaseException:
            db.rollback()
            raise
    after = journal_observation(candidate)
    relation = require_journal_preserved(before, after)
    if digest(inventory_path) != before_sha:
        raise AssertionError('Original running-source inventory changed')
    # The actual current journal entrypoint authenticates target DDL and reads
    # the original operations/states. It returns no original admission receipt.
    from agent_comms.compaction_journal import CompactionJournal
    journal = CompactionJournal(candidate)
    with journal.transaction() as db:
        from agent_comms.compaction_records import SelectedSummaryAttempt
        attempts = SelectedSummaryAttempt.select(db)
        if len(attempts) != len(requests):
            raise AssertionError('Current journal omitted an original attempt')
    result = {'classification':'private-operator-control-from-running-source-inventory-not-stopped-carry',
              'inventory_sha256':before_sha, 'candidate_sha256':digest(candidate),
              'original_release':list(original.release_versions),
              'target_release':list(target.release_versions),
              'relation':relation, 'carry':evidence,
              'provider_calls':0, 'native_inputs':0, 'owner_signals':0}
    write_original(base/'receipt.json', (json.dumps(result,indent=2)+'\n').encode())
    return result


def run(base, source_python, root):
    """Final installed operator control; caller provides an actual stopped copy."""
    base.mkdir(mode=0o700, exist_ok=True)
    PrivateDirectoryRole.require(base.lstat())
    PrivateDirectoryRole.require(root.lstat())
    if not root.resolve(strict=True).is_relative_to(base.resolve(strict=True)) or root == base:
        raise ValueError('Stopped original copy must be private under this owned control root')
    if (base/'receipt.json').exists() or (base/'receipt.json').is_symlink():
        raise ValueError('Control receipt must be fresh')
    original = original_declaration(source_python)
    source_hashes = {path.name:digest(path) for path in RuntimeNativeFiles(root).paths
                     if path.exists()}
    before_journal = journal_observation(root/'compaction-commits.sqlite3')
    if not before_journal['selected_summary_attempts']:
        raise ValueError('Actual historical selected summary evidence is required')
    plan = prepare(root, base/'matched-candidate', original, source_python)
    if source_hashes != {name:digest(root/name) for name in source_hashes}:
        raise AssertionError('Original stores changed during candidate preparation')
    installed = CarryNativeRuntimeInstallation(plan)
    if FieldCodec.decode(RuntimeInstallation, FieldCodec.encode(installed)) != installed:
        raise AssertionError('Canonical runtime installation declaration does not round-trip')
    refused = []
    # One-use custody refusals operate on ONLY this private copy, with exact
    # original bytes restored afterward. No original session or proof is edited.
    for case in ('candidate-change','existing-attempt','companion'):
        try:
            if case == 'candidate-change':
                path=plan.candidate/'compaction-commits.sqlite3'
                prior=path.read_bytes()
                path.write_bytes(prior+b'changed')
                try:
                    plan.require_candidate()
                finally:
                    path.write_bytes(prior)
            elif case == 'existing-attempt':
                destination=base/'preexisting-attempt'
                destination.mkdir(mode=0o700)
                plan.install(destination)
            else:
                path=root/'compaction-commits.sqlite3-wal'
                write_original(path,b'owned control companion')
                try:
                    with RuntimeNativeFiles(root).acquire():
                        pass
                finally:
                    path.unlink()
        except (ValueError,RuntimeError,FileExistsError):
            refused.append(case)
        else:
            raise AssertionError('Expected stopped custody refusal missing: '+case)
        if source_hashes != {name:digest(root/name) for name in source_hashes}:
            raise AssertionError('Custody refusal changed original stores')
    with RuntimeCompactionFiles(root).acquire() as acquired:
        receipt = installed.install(acquired, base/'original-preimages')
    if any(digest(base/'original-preimages'/name) != sha for name,sha in source_hashes.items()):
        raise AssertionError('Original preimages were not retained exactly')
    relation=require_journal_preserved(before_journal,journal_observation(root/'compaction-commits.sqlite3'))
    result={'classification':'private-stopped-copy-installed-operator-control',
            'original_release':list(original.release_versions),
            'target_release':list(plan.target.release_versions),
            'relation':relation, 'custody_refusals':refused, 'installation':receipt,
            'provider_calls':0, 'native_inputs':0, 'owner_signals':0,
            'public_cutover_qualified':False}
    write_original(base/'receipt.json',(json.dumps(result,indent=2)+'\n').encode())
    return result


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('base',type=Path)
    parser.add_argument('--source-python',required=True,type=Path)
    source=parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--stopped-original',type=Path)
    source.add_argument('--running-journal-inventory',type=Path)
    args=parser.parse_args()
    if args.running_journal_inventory:
        result=run_journal_inventory(args.base.absolute(),args.source_python,args.running_journal_inventory)
    else:
        result=run(args.base.absolute(),args.source_python,args.stopped_original.absolute())
    print(json.dumps(result,indent=2))
