"""Activate the reviewed Toad214/220 pair without changing backend authority."""
import importlib.metadata as metadata
import json
import os
from pathlib import Path

old = Path('/home/ts/.local/share/agent-comms/runtime-canonical-native-checkpoint-20260929')
new = Path('/home/ts/.local/share/agent-comms/runtime-canonical-launch-admission-20260929')
receipt = json.loads((old / 'activation.json').read_text())
receipt['toad'] = '540bc85c29f25bfb434346950cafff7688d9ef8e'
for distribution, key in [('agent-comms', 'core'), ('batrachian-toad', 'toad'), ('textual', 'textual')]:
    provenance = json.loads(metadata.distribution(distribution).read_text('direct_url.json'))
    assert provenance['vcs_info']['commit_id'] == receipt[key]
    assert not provenance.get('dir_info', {}).get('editable', False)
from agent_comms.active_route import read_active_route
assert str(read_active_route().native_package) == receipt['native_package']
import agent_comms.acp
import toad.acp.agent_process
import toad.widgets.viewport_body
links = {name: Path('/home/ts/.local/bin') / name for name in receipt['launcher_names']}
for name, link in links.items():
    assert link.is_symlink() and link.resolve() == (old / 'bin' / name).resolve(), name
    assert (new / 'bin' / name).is_file(), name
(new / 'activation.json').write_text(json.dumps(receipt, indent=2) + '\n')
for name, link in links.items():
    temporary = link.with_name(link.name + '.launch-scroll-activation')
    assert not temporary.exists() and not temporary.is_symlink()
    temporary.symlink_to(new / 'bin' / name)
    os.replace(temporary, link)
Path(__file__).with_name('launch-scroll-activation.json').write_text(json.dumps(receipt, indent=2) + '\n')
print('DEFAULT_LAUNCH_SCROLL_CHECKPOINT_ACTIVATED')
