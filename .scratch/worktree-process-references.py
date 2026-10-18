"""Read-only privileged /proc reference scan. Never records raw environment."""
import json
import os
import re
from pathlib import Path
import sys

roots = json.load(sys.stdin)
references = {root: [] for root in roots}
patterns = {root: re.compile(re.escape(root) + r"(?=$|[/\s\x00\"'])") for root in roots}
errors = []
for directory in Path('/proc').iterdir():
    if not directory.name.isdigit():
        continue
    fields = {}
    try:
        uid = directory.stat().st_uid
        for name in ('cwd', 'exe'):
            try:
                fields[name] = os.readlink(directory / name)
            except (FileNotFoundError, ProcessLookupError):
                pass
            except OSError as error:
                errors.append([directory.name, name, str(error)])
        for name in ('maps', 'cmdline', 'environ'):
            try:
                fields[name] = (directory / name).read_bytes().decode(errors='replace')
            except (FileNotFoundError, ProcessLookupError):
                pass
            except OSError as error:
                errors.append([directory.name, name, str(error)])
    except (FileNotFoundError, ProcessLookupError):
        continue
    owner = None
    for entry in fields.get('environ', '').split('\0'):
        if entry.startswith('AGENT_COMMS_THREAD='):
            owner = entry.partition('=')[2]
    for root in roots:
        hits = [name for name, text in fields.items() if patterns[root].search(text)]
        if hits:
            references[root].append({'pid': int(directory.name), 'uid': uid, 'owner': owner, 'fields': hits})
print(json.dumps({'references': references, 'errors': errors}))
