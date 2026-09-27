"""Authored transaction migration; exact class patch, not an equivalence proof."""
import ast
import json
from pathlib import Path

path = Path('src/agent_comms/relationships.py').resolve()
source = path.read_text()
node = next(n for n in ast.parse(source).body if isinstance(n, ast.ClassDef) and n.name == 'ThreadRelationships')
old = '\n'.join(source.splitlines()[node.lineno - 1:node.end_lineno])
new = old.replace('self.path = comms.root / "relationships.json"', 'self.store = RelationshipStore(comms.root / RelationshipStore.filename)')
a = new.index('    def _load(')
b = new.index('    def _canonical_edges(', a)
new = new[:a] + '''    @property
    def path(self) -> Path:
        return self.store.path

''' + new[b:]
new = new.replace('Collaboration(**raw)', 'FieldCodec.decode(Collaboration, raw)')
new = new.replace('with _store_lock(self.comms._wire_lock_path), _store_lock(self.path):', 'with _store_lock(self.comms._wire_lock_path):')
new = new.replace('self._load()', 'self.store.read()')
a = new.index('    def edit(')
b = new.index('    def set_order(', a)
edit = new[a:b]
edit = edit.replace('            state = self.store.read()\n', '')
# Hold the wire fence through the complete store transaction and compute the public result there.
x = edit.index('            edges = ')
body = edit[x:]
body = body.replace('                    state["collaborations"] = [\n                        asdict(edge) for edge in edges if self._pair_identity(edge) != identity\n                    ]\n                    self._save(state)\n                return None', '''                    return {**state, "collaborations": [
                        FieldCodec.encode(edge)
                        for edge in edges if self._pair_identity(edge) != identity
                    ]}
                return state''')
body = body.replace('                return self._orient(self._unique_edges(active)[0], first)', '                result = self._orient(self._unique_edges(active)[0], first)\n                return state')
body = body.replace('            state["collaborations"] = [asdict(edge) for edge in edges]\n            self._save(state)\n            return self._orient(result, first)', '            result = self._orient(result, first)\n            return {**state, "collaborations": [FieldCodec.encode(edge) for edge in edges]}')
body = '\n'.join('    '+line if line else '' for line in body.splitlines())
edit = edit[:x] + '''            result: Collaboration | None = None

            def change(state: dict[str, Any]) -> dict[str, Any]:
                nonlocal result
''' + body + '''

            self.store.update(change)
            return result

'''
new = new[:a] + edit + new[b:]
a = new.index('            state = self.store.read()', new.index('    def set_order('))
b = new.index('        return order', a)
new = new[:a] + '''            def change(state: dict[str, Any]) -> dict[str, Any]:
                rows = [
                    row for row in state["orders"]
                    if not (
                        registry.aliases.get(row["owner"], row["owner"]) == thread.name
                        and row["owner_created"] == thread.created_at
                        and row["group"] == group
                    )
                ]
                rows.append(FieldCodec.encode(RelationshipOrder(
                    thread.name, thread.created_at, group, order
                )))
                return {**state, "orders": rows}

            self.store.update(change)
''' + new[b:]
plan = {"recipes": [{"recipe_id": "relationship-owned-store-transactions", "operations": [{"operation": "patch_target", "file_path": str(path), "target_qualname": "ThreadRelationships", "replacements": [{"old_source": old, "new_source": new}]}]}]}
Path('evidence/refactor-a8-relationship-awareness/relationships-plan.json').write_text(json.dumps(plan, indent=2)+'\n')
