"""Move persistence to LockedStore; retain the legacy optional envelope verbatim."""
import ast
import json
from pathlib import Path

path = Path('src/agent_comms/passive_channel_awareness.py').resolve()
source = path.read_text()
node = next(n for n in ast.parse(source).body if isinstance(n, ast.ClassDef) and n.name == 'PassiveChannelAwareness')
old = '\n'.join(source.splitlines()[node.lineno - 1:node.end_lineno])
new = old.replace('self.path = root / "acp_passive_channel_awareness.json"', 'self.store = PassiveAwarenessStore(root / PassiveAwarenessStore.filename)')
a = new.index('    def _read(')
b = new.index('    def initialize(', a)
read = new[a:new.index('    def _write(', a)]
# Retain exact legacy row validation as the document owner's boundary hook.
validation = read[read.index('        if type(payload)'):]
validation = validation.replace('        return rows', '        return super()._decode(payload)')
store = '''class PassiveAwarenessStore(LockedStore[dict[str, Any] | None]):
    """Optional advisory document; malformed storage is never repaired implicitly.

    The versioned envelope and rows are extensible external mappings. Preserve
    unknown keys through updates; their validation belongs to this boundary.
    """

    filename = "acp_passive_channel_awareness.json"
    json_sort_keys = True

    @property
    def record_type(self) -> type[dict[str, Any]]:
        return dict[str, Any]

    def empty(self) -> dict[str, Any]:
        return {"version": 1, "rows": {}}

    def _unreadable(self, error: Exception) -> None:
        return None

    def _decode(self, payload: Any) -> dict[str, Any] | None:
''' + validation
Path('.artifacts/passive-store.txt').write_text(store)
new = new[:a] + '''    @property
    def path(self) -> Path:
        return self.store.path

''' + new[b:]
# Initialize/scope mutations execute under one inherited exclusive transaction.
for method, next_method in [('initialize', 'scope_changed'), ('scope_changed', 'sources')]:
 a = new.index('    def '+method+'(')
 b = new.index('    def '+next_method+'(', a)
 part = new[a:b]
 x = part.index('        with _store_lock(self.path):')
 body = part[x:].replace('        with _store_lock(self.path):\n            rows = self._read()', '''        def change(document: dict[str, Any] | None) -> dict[str, Any] | None:
            if document is None:
                return document
            rows = dict(document["rows"])''')
 body = body.replace('            if rows is None:\n                return  # Corrupt ledger fails closed without suppressing an authorized turn.\n', '')
 body = body.replace('            if rows is None:\n                return\n', '')
 body = body.replace('            row = rows.get(key)', '            saved = rows.get(key)\n            row = dict(saved) if saved is not None else None')
 body = body.replace('            row = rows.get(self._key(owner))', '            saved = rows.get(self._key(owner))\n            row = dict(saved) if saved is not None else None')
 body = body.replace('                return\n', '                return document\n')
 body = body.replace('                self._write(rows)', '                return {**document, "rows": rows}', 1) if method == 'initialize' else body
 body = body.replace('                self._write(rows)', '                rows[key] = row\n                return {**document, "rows": rows}')
 body = body.replace('            self._write(rows)', '            rows[self._key(owner)] = row\n            return {**document, "rows": rows}')
 if method == 'initialize':
  body = body.rstrip() + '\n            return document\n'
 part = part[:x] + body.rstrip() + '\n\n        self.store.update(change)\n\n'
 new = new[:a]+part+new[b:]
new = new.replace('with _store_lock(self.path):\n            rows = self._read()', 'with self.store.reading() as document:\n            rows = document["rows"] if document is not None else None')
# Frame selection and witness publication remain in one exclusive transaction.
a = new.index('    def frame(')
part = new[a:]
x = part.index('        with self.store.reading() as document:')
part = part[:x] + part[x:].replace('        with self.store.reading() as document:', '''        frame = ""

        def change(document: dict[str, Any] | None) -> dict[str, Any] | None:
            nonlocal frame''', 1)
part = part.replace('                return ""', '                return document')
part = part.replace('            row = rows.get(self._key(owner))', '            key = self._key(owner)\n            row = rows.get(key)')
part = part.replace('                        if latest != row["known"]:\n                            row["known"] = latest\n                            self._write(rows)', '''                        changed = document
                        if latest != row["known"]:
                            changed = {**document, "rows": {
                                **rows, key: {**row, "known": latest}
                            }}''')
part = part.replace('                        return frame if len(frame.encode()) <= _MAX_FRAME_BYTES else ""', '''                        if len(frame.encode()) > _MAX_FRAME_BYTES:
                            frame = ""
                        return changed''')
part += '''
        try:
            self.store.update(change)
        except (OSError, ValueError, TypeError):
            return ""
        return frame
'''
new = new[:a]+part
plan={"recipes":[{"recipe_id":"passive-owned-store-transactions", "operations":[{"operation":"patch_target","file_path":str(path),"target_qualname":"PassiveChannelAwareness","replacements":[{"old_source":old,"new_source":new}]}]}]}
Path('evidence/refactor-a8-relationship-awareness/passive-plan.json').write_text(json.dumps(plan,indent=2)+'\n')
