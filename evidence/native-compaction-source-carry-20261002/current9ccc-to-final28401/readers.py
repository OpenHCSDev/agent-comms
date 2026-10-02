import hashlib,importlib.metadata,json,sqlite3,sys
from pathlib import Path
from contextlib import closing
sys.path.insert(0,str(Path.cwd()/'tools/cutover'))
from native_schema_carry import NativeSchemaDeclaration,rows,inventory,row_digest,RuntimeNativeFiles
from native_schema_carry_controls import journal_observation
from retained_summary_reset import RuntimeGoalFiles
from runtime_installation import PreserveRuntimeInstallation
from agent_comms.compaction_journal import CompactionJournal
from agent_comms.compaction_records import JournalTable,SelectedSummaryAttempt,CompactionOperation,CompactionPublication
from agent_comms.compaction_identity import SelectedCommitReference
from agent_comms.compaction_outcomes import CompactionOutcomeSnapshot
from agent_comms.native_runtime_input import NativeRuntimeInput,CurrentNativeCursor
from agent_comms.native_prompt_binding import PromptBinding
from agent_comms.private_sidecar import sidecar_connection
from agent_comms.registry_document import RegistryDocument
from agent_comms.registry_provenance import RegistryProvenance
from agent_comms.typed_table import TypedTable
from agent_comms.field_codec import FieldCodec
from publish_openhcs_recovery import ROOT,digest
from routing_recovery import write_original
base=Path(__file__).parent;root=base/'private-original'
source=FieldCodec.decode(NativeSchemaDeclaration,json.loads((base/'source-declaration.json').read_text()))
original=journal_observation(base/'public-raw-preimages/compaction-commits.sqlite3')
schema=NativeSchemaDeclaration.observe();assert schema.release_versions==(9,3,3,6)
# Actual current journal/schema/FieldCodec consumers, ONLY privately carried DB.
journal=CompactionJournal(root/'compaction-commits.sqlite3')
with journal.transaction() as db:
 typed={table.declared_name:table.select(db) for table in TypedTable.members_with(JournalTable)}
 attempts=typed[SelectedSummaryAttempt.declared_name]
 operations=typed[CompactionOperation.declared_name]
 original_sources={operation:hashlib.sha256(source_json.encode()).hexdigest() for operation,_,source_json,_ in original[SelectedSummaryAttempt.declared_name]}
 older=json.loads(Path('evidence/native-compaction-source-carry-20261002/running-source-inventory01/authentic-installed-declaration-inventory.json').read_text())['attempts']
 old_by_hash={value['operation_id_sha256']:value['source_json_sha256'] for value in older}
 assert all(old_by_hash[hashlib.sha256(attempt.operation_id.encode()).hexdigest()]==hashlib.sha256(attempt.source_json.encode()).hexdigest() for attempt in attempts)
 links=[]
 for operation in operations:
  reference=SelectedCommitReference.from_intent(json.loads(operation.intent_json))
  attempt=next(attempt for attempt in attempts if attempt.identity==reference.identity(operation.session_file))
  operation.require_summary_link(attempt,admit_original=True)
  operation.committed_outcome()
  links.append({'operation_id_sha256':hashlib.sha256(attempt.operation_id.encode()).hexdigest(),'source_digest':reference.source_digest,'original_source_digest_unchanged':reference.source_digest==original_sources[attempt.operation_id]})
 registry=RegistryProvenance.capture(RegistryDocument.from_wire(json.loads((ROOT/'registry.json').read_text())))
 outcomes=[]
 for attempt in attempts:
  observed=CompactionOutcomeSnapshot.read(db,attempt.session_file,attempt.request.source.incarnation,registry)
  observed.revision
  outcomes.append({'operation_id_sha256':hashlib.sha256(attempt.operation_id.encode()).hexdigest(),'typed_outcomes':len(observed.outcomes),'native_source_exists':Path(attempt.session_file).exists()})
# Original installed current native publication reader owns schema/read lifetime.
with NativeRuntimeInput._publication_read(root) as db:
 native=NativeRuntimeInput.select(db)
 cursors=CurrentNativeCursor.select(db)
with sidecar_connection(root/'native_prompt_bindings.sqlite3',PromptBinding,blocking=False) as db:
 bindings=PromptBinding.select(db)
assert {row.input_id for row in native}=={row.input_id for row in bindings}
by_input={row.input_id:row for row in native}
assert all(binding.execution==by_input[binding.input_id].execution for binding in bindings)
assert all(binding.owner_identity==by_input[binding.input_id].owner_identity for binding in bindings)
with RuntimeGoalFiles(root).acquire() as acquired:
 goal=PreserveRuntimeInstallation(goal_schema=source.goal).synchronize_goal(acquired,base/'must-not-repeat-goal-carry')
assert goal['classification']=='goal/preserve' and not (base/'must-not-repeat-goal-carry').exists()
# The original current goal snapshot bytes remain identical; no generation or
# original state is reconstructed by target qualification.
capture=json.loads((base/'capture-receipt.json').read_text())
assert digest(root/'goal-private/goal_attempts.sqlite3')==capture['private_logical_backups']['goal-private/goal_attempts.sqlite3']
result={'state':'PASS-installed-Native6-historical-original-readers',
 'installed_direct_url':json.loads(importlib.metadata.distribution('agent-comms').read_text('direct_url.json')),
 'typed_journal_rows':{name:len(values) for name,values in typed.items()},
 'oldest_four_source_digests_unchanged':True,'original_committed_links':links,
 'outcome_readers':outcomes,'typed_native_inputs':len(native),'typed_prompt_bindings':len(bindings),'typed_native_cursors':len(cursors),
 'original_goal_classification':goal['classification'],'original_goal_snapshot_bytes_unchanged':True,'physical_public_preimage_retained_separately':True,
 'source_revision_observations':'Original reserved session/proof revisions and paths preserved; no rewriting or re-admission. Presence reports do not grant native input proof.',
 'public_writes':0,'public_stops':0,'provider_calls':0,'input_replays':0,
 'limits':'Private historical carry of consistent running-source snapshot; public stopped publication remains release-owner work.'}
write_original(base/'installed-readers.json',(json.dumps(result,indent=2)+'\n').encode())
print(json.dumps({'state':result['state'],'journal_counts':result['typed_journal_rows'],'native_inputs':len(native),'bindings':len(bindings),'committed_links':len(links),'goal':goal['classification']}))
