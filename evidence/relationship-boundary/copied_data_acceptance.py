import json
from pathlib import Path
from agent_comms.field_codec import FieldCodec
from agent_comms.relationship_migration import VersionOneRelationships, migrate_relationships
from agent_comms.relationships import RelationshipDocument, RelationshipStore
from agent_comms.passive_channel_awareness import PassiveAwarenessStore
from agent_comms.registry_store import RegistryStore

source = Path('/home/ts/.agent-comms')
owned = Path('.artifacts/relationship-boundary/copied-source')
owned.mkdir()
for name in ('relationships.json', 'registry.json', 'acp_passive_channel_awareness.json'):
    (owned / name).write_bytes((source / name).read_bytes())
registry_store = RegistryStore(owned / 'registry.json')
registry = registry_store._decode(json.loads(registry_store.path.read_text())).snapshot()
old = FieldCodec.decode(VersionOneRelationships, json.loads((owned / 'relationships.json').read_text()))
new = migrate_relationships(owned / 'relationships.json', registry)
assert len(old.collaborations) == len(new.collaborations) == 17
assert len(old.orders) == len(new.orders) == 2
assert all(any(original == edge.revision() or original in edge.history for edge in new.collaborations)
           for original in old.collaborations)
assert FieldCodec.decode(RelationshipDocument, FieldCodec.encode(new)) == new
assert RelationshipStore(owned / 'relationships.json').read() == new
passive_path = owned / PassiveAwarenessStore.filename
payload = json.loads(passive_path.read_text())
passive = PassiveAwarenessStore(passive_path).read()
assert passive is not None
assert FieldCodec.encode(passive) == payload
report = {'source_root': str(source), 'writes_only_to': str(owned),
          'relationship_records': len(old.collaborations), 'sort_preferences': len(old.orders),
          'all_original_records_preserved': True,
          'historical_records': sum(len(edge.history) for edge in new.collaborations),
          'passive_rows': len(passive.rows),
          'passive_witnesses': sum(len(row.known) for row in passive.rows.values()),
          'passive_exact_roundtrip': True}
Path('evidence/relationship-boundary/copied-data-acceptance.json').write_text(json.dumps(report, indent=2)+'\n')
print(json.dumps(report))
