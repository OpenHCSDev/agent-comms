"""Mechanical private-API rename from the completed AST owner/consumer census."""
import ast
import re
import subprocess
from pathlib import Path

root = Path.cwd()
paths = [Path(p) for p in subprocess.check_output(['git','ls-files','-z']).decode().split('\0') if p.endswith('.py')]
# These production modules consume PersistentPiSession's state. The selected
# source and per-turn resource stack below own different facts and get names
# for those facts, rather than blindly replacing every occurrence of a noun.
state_modules = {'backend.py','native_custody.py','pi_native_backend.py','native_session_prepare.py','coordinated_runtime.py','turn_output.py'}
unrelated_tests = {'test_owner_partial_retirement.py','shared_bus_restart_native.py'}
changed = []
for path in paths:
    old = path.read_text()
    new = old.replace('native_custody_retirement', 'pi_session_retirement')
    new = new.replace('native_custody', 'pi_session_state').replace('NativeCustody', 'PiSessionState')
    if path.parts[0] == 'src' and path.name in state_modules:
        new = new.replace('.custody', '.native_state')
        new = re.sub(r'\bcustody\b', 'ownership', new, flags=re.IGNORECASE)
    elif path == Path('src/agent_comms/selected_session.py'):
        new = new.replace('self.custody.custody', 'self.pi_session.native_state')
        new = new.replace('self.custody', 'self.pi_session')
        new = new.replace('custody:', 'pi_session:').replace('custody=', 'pi_session=')
        new = new.replace("# The owner's native custody for this source. Custody only; it never takes\n    # part in the launch key.", "# The owner's live Pi session for this source; not part of the launch key.")
    elif path == Path('src/agent_comms/tracked_turn.py'):
        new = re.sub(r'\bcustody\b', 'resources', new)
    elif path.parts[0] == 'tests' and path.name not in unrelated_tests:
        new = new.replace('.custody', '.native_state')
    if new != old:
        ast.parse(new, filename=str(path))
        path.write_text(new)
        changed.append(str(path))
print('\n'.join(changed))
