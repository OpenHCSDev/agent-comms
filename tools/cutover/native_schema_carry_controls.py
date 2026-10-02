"""Installed stopped-copy carry controls using actual original Native5 stores.

The release owner supplies a stopped, privately retained original root. This
control neither seeds history/proofs nor stops, launches, retries or prompts an
owner. Running-source journal inventory can exercise request conversion only;
that mode explicitly does not qualify the public stopped-custody installation.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

from agent_comms.field_codec import FieldCodec
from agent_comms.private_path import PrivateDirectoryRole
from native_schema_carry import (
    NativeSchemaDeclaration, NativeSchemaCarryPlan, RuntimeNativeFiles,
    carry_compaction, inventory, row_digest,
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


def require_journal_preserved(before, after, original, target):
    from agent_comms.compaction_records import SelectedSummaryAttempt

    name = SelectedSummaryAttempt.declared_name
    if original.version == target.version:
        if any(after[key] != values for key, values in before.items()):
            raise AssertionError('Original journal facts changed during additive carry')
        added = target.compaction_columns.keys() - original.compaction_columns.keys()
        if after.keys() != before.keys() | added or any(after[key] for key in added):
            raise AssertionError('Added declaration members must remain empty')
        return {'rows': {key: len(values) for key, values in before.items()},
                'unchanged_tables_sha256': row_digest(before), 'created_empty_tables': sorted(added)}
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
    original.require_carry_target(target)
    before_sha = digest(inventory_path)
    candidate = base/'compaction-commits.sqlite3'
    shutil.copyfile(inventory_path, candidate)
    candidate.chmod(0o600)
    requests = target.capture_requests(candidate, source_python, original)
    before = journal_observation(candidate)
    with closing(sqlite3.connect(candidate)) as db, db:
        db.execute('PRAGMA foreign_keys=OFF')
        db.execute('PRAGMA synchronous=FULL')
        db.execute('BEGIN IMMEDIATE')
        evidence = carry_compaction(db, original, target, requests)
    after = journal_observation(candidate)
    relation = require_journal_preserved(before, after, original, target)
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
    installed = CarryNativeRuntimeInstallation(
        original=original,
        source_python=source_python, candidate=base/'matched-candidate')
    if FieldCodec.decode(RuntimeInstallation, FieldCodec.encode(installed)) != installed:
        raise AssertionError('Canonical runtime installation declaration does not round-trip')
    if installed.original_goal() is not installed.original.goal:
        raise AssertionError('Carry goal declaration is not derived from original source')
    refused = []
    # One-use custody refusals operate on ONLY this private copy, with exact
    # original bytes restored afterward. No original session or proof is edited.
    for case in ('existing-candidate','existing-attempt','companion'):
        try:
            member = replace(installed, candidate=base/(case+'-candidate'))
            if case == 'existing-candidate':
                member.candidate.mkdir(mode=0o700)
                with RuntimeCompactionFiles(root).acquire() as acquired:
                    member.install(acquired, base/'existing-candidate-preimages')
            elif case == 'existing-attempt':
                destination=base/'preexisting-attempt'
                destination.mkdir(mode=0o700)
                with RuntimeCompactionFiles(root).acquire() as acquired:
                    member.install(acquired, destination)
            else:
                path=root/'compaction-commits.sqlite3-wal'
                write_original(path,b'owned control companion')
                try:
                    with RuntimeCompactionFiles(root).acquire() as acquired:
                        member.install(acquired, base/'companion-preimages')
                finally:
                    path.unlink()
        except (ValueError,RuntimeError,FileExistsError):
            refused.append(case)
        else:
            raise AssertionError('Expected stopped custody refusal missing: '+case)
        if source_hashes != {name:digest(root/name) for name in source_hashes}:
            raise AssertionError('Custody refusal changed original stores')
    with RuntimeCompactionFiles(root).acquire() as acquired:
        protected = frozenset(root/name for name in source_hashes)
        if installed.unchanged_protected(protected, acquired):
            raise AssertionError('Native declared members remain in unchanged partition')
        if installed.candidate.exists():
            raise AssertionError('Native candidate was derived before installation custody')
        receipt = installed.install(acquired, base/'original-preimages')
    plan = FieldCodec.decode(NativeSchemaCarryPlan, json.loads(
        (base/'original-preimages/reviewed-carry.json').read_text()))
    path=plan.candidate/'compaction-commits.sqlite3'
    prior=path.read_bytes()
    path.write_bytes(prior+b'changed')
    try:
        try:
            plan.require_candidate()
        except (ValueError,RuntimeError):
            refused.append('candidate-change')
        else:
            raise AssertionError('Changed installed candidate was accepted')
    finally:
        path.write_bytes(prior)
    if any(digest(base/'original-preimages'/name) != sha for name,sha in source_hashes.items()):
        raise AssertionError('Original preimages were not retained exactly')
    relation=require_journal_preserved(before_journal,journal_observation(root/'compaction-commits.sqlite3'),
                                      original, plan.target)
    from agent_comms.compaction_journal import CompactionJournal
    from agent_comms.compaction_records import JournalTable
    from agent_comms.typed_table import TypedTable
    with CompactionJournal(root/'compaction-commits.sqlite3').transaction() as db:
        installed_rows = {table.declared_name: len(table.select(db))
                          for table in TypedTable.members_with(JournalTable)}
    result={'classification':'private-stopped-copy-installed-operator-control',
            'original_release':list(original.release_versions),
            'target_release':list(plan.target.release_versions),
            'relation':relation, 'installed_typed_rows':installed_rows,
            'custody_refusals':refused, 'installation':receipt,
            'provider_calls':0, 'native_inputs':0, 'owner_signals':0,
            'public_cutover_qualified':False}
    write_original(base/'receipt.json',(json.dumps(result,indent=2)+'\n').encode())
    return result


def goal_journey(base, original_db, declaration):
    """Actual original ledger -> preserving member -> strict target store."""
    from contextlib import closing
    import sqlite3
    from native_schema_carry import inventory, row_digest, rows
    from retained_summary_reset import RuntimeGoalFiles
    from runtime_installation import PreserveRuntimeInstallation
    from publish_openhcs_recovery import retain_file
    from agent_comms.goal_attempts import GoalAttemptSchema, GoalAttemptStore, StorageUncertainError
    base = base.absolute()
    base.mkdir(mode=0o700)
    root = base / 'root'
    root.mkdir(mode=0o700)
    goal_root = root / 'goal-private'
    goal_root.mkdir(mode=0o700)
    path = goal_root / 'goal_attempts.sqlite3'
    retain_file(original_db, path)
    original_sha = digest(path)
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)) as db:
        before = inventory(db)
    try:
        GoalAttemptStore(goal_root)
    except StorageUncertainError:
        pass
    else:
        raise AssertionError('Original incompatible ledger unexpectedly admitted')
    source_schema = json.loads(declaration.read_text())
    installation = PreserveRuntimeInstallation(goal_schema=source_schema)
    assert FieldCodec.decode(RuntimeInstallation, FieldCodec.encode(installation)) == installation
    # Wrong declaration must not turn into a guessed migration.
    with RuntimeGoalFiles(root).acquire() as acquired:
        try:
            PreserveRuntimeInstallation(goal_schema={}).synchronize_goal(acquired, base/'wrong-preimage')
        except ValueError:
            pass
        else:
            raise AssertionError('Unknown goal declaration was transformed')
        assert digest(path) == original_sha
        receipt = installation.synchronize_goal(acquired, base/'originals')
    assert digest(base/'originals/goal-private/goal_attempts.sqlite3') == original_sha
    store = GoalAttemptStore(goal_root)
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)) as db:
        after = inventory(db)
        marker = GoalAttemptSchema.select(db)
    facts = before.keys() - {GoalAttemptSchema.declared_name}
    assert all(before[name] == after[name] for name in facts)
    assert marker == [GoalAttemptSchema.current()]
    for goal_id, *_ in before['generation']:
        assert store.snapshot(goal_id) is not None
    with RuntimeGoalFiles(root).acquire() as acquired:
        matched = installation.synchronize_goal(acquired, base/'must-not-recarry')
        acquired.require_original()
    assert not (base/'must-not-recarry').exists()
    result = {'state': 'original-goal-ledger-carry-passed', 'target_python': sys.executable,
              'target_package': __import__('agent_comms').__file__,
              'original_db': str(original_db), 'original_copy_sha256': original_sha,
              'source_declaration_sha256': digest(declaration), 'installation': receipt,
              'unchanged_fact_rows': sum(len(before[name]) for name in facts),
              'unchanged_fact_rows_sha256': row_digest({name: before[name] for name in sorted(facts)}),
              'matched_declaration_preserved': matched,
              'refusals': ['original-strict-schema', 'unauthenticated-declaration'],
              'native_inputs': 0, 'provider_calls': 0, 'public_mutations': 0, 'owner_stops': 0,
              'strength': 'copied running-source inventory; actual RuntimeInstallation and strict GoalAttemptStore; not stopped public carry or native/UI journey'}
    (base/'receipt.json').write_text(json.dumps(result, indent=2)+'\n')
    return {'state': result['state'], 'receipt': str(base/'receipt.json')}


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('base',type=Path)
    parser.add_argument('--source-python',type=Path)
    source=parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--stopped-original',type=Path)
    source.add_argument('--running-journal-inventory',type=Path)
    source.add_argument('--goal-original-db',type=Path)
    parser.add_argument('--goal-original-declaration',type=Path)
    args=parser.parse_args()
    if args.goal_original_db:
        if not args.goal_original_declaration:
            parser.error('Goal carry requires authentic original declaration')
        result=goal_journey(args.base,args.goal_original_db,args.goal_original_declaration)
    elif not args.source_python:
        parser.error('Native carry requires authentic source interpreter')
    elif args.running_journal_inventory:
        result=run_journal_inventory(args.base.absolute(),args.source_python,args.running_journal_inventory)
    else:
        result=run(args.base.absolute(),args.source_python,args.stopped_original.absolute())
    print(json.dumps(result,indent=2))
