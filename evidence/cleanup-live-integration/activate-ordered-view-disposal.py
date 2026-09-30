"""One reviewed Toad-only cutover; no backend restart or history conversion."""
import importlib.metadata as metadata
import json
import os
from pathlib import Path

from agent_comms.active_route import read_active_route

old = Path('/home/ts/.local/share/agent-comms/runtime-canonical-launch-admission-20260929')
new = Path('/home/ts/.local/share/agent-comms/runtime-ordered-view-disposal-20260929')
receipt_path = Path(__file__).with_name('ordered-view-disposal-activation.json')
assert not receipt_path.exists(), 'Inspect a recorded attempt instead of replaying it'
receipt = json.loads((old / 'activation.json').read_text())
receipt['toad'] = '6660cb6854bda7af056865326e1bb494f4690faf'
for distribution, key in [('agent-comms', 'core'), ('batrachian-toad', 'toad'), ('textual', 'textual')]:
    provenance = json.loads(metadata.distribution(distribution).read_text('direct_url.json'))
    assert provenance['vcs_info']['commit_id'] == receipt[key]
    assert not provenance.get('dir_info', {}).get('editable', False)
assert str(read_active_route().native_package) == receipt['native_package']
import agent_comms.acp
import toad.session_presentation
import toad.workspace_sessions
links = {name: Path('/home/ts/.local/bin') / name for name in receipt['launcher_names']}
for name, link in links.items():
    assert link.is_symlink() and link.resolve() == (old / 'bin' / name).resolve(), name
    assert (new / 'bin' / name).is_file(), name
attempt = {'state': 'reviewed-before-link-cutover', 'activation': receipt}
receipt_path.write_text(json.dumps(attempt, indent=2) + '\n')
(new / 'activation.json').write_text(json.dumps(receipt, indent=2) + '\n')
for name, link in links.items():
    temporary = link.with_name(link.name + '.ordered-disposal-activation')
    assert not temporary.exists() and not temporary.is_symlink()
    temporary.symlink_to(new / 'bin' / name)
    os.replace(temporary, link)
attempt['state'] = 'activated-not-yet-default-entrypoint-verified'
receipt_path.write_text(json.dumps(attempt, indent=2) + '\n')
print('DEFAULT_ORDERED_VIEW_DISPOSAL_ACTIVATED')
