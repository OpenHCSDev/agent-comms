"""One reviewed operator run; retains original inputs and per-owner launch settings."""
import fcntl
import importlib.metadata as metadata
import json
import os
import shlex
from dataclasses import replace
from pathlib import Path

from agent_comms.active_route import active_route_path, read_active_route, _publish_active_route_locked
from agent_comms.comms import wire
from agent_comms.owner_lifecycle import OwnerRestartSelection
from agent_comms.private_nk_entrypoint import PACKAGE_ENV, ROOT_ID_ENV
from agent_comms.store_files import _store_lock

old = Path('/home/ts/.local/share/agent-comms/runtime-retained-native-history-20260929')
new = Path('/home/ts/.local/share/agent-comms/runtime-canonical-native-checkpoint-20260929')
package = Path('/home/ts/.local/share/agent-comms/native-current-4ab910061590d1e0/node_modules/@earendil-works/pi-coding-agent')
previous = read_active_route()
receipt_path = Path(__file__).with_name('paired-activation.json')
assert not receipt_path.exists(), 'Do not replay an operator attempt'
receipt = json.loads((old / 'activation.json').read_text())
assert str(previous.native_package) == receipt['native_package']
receipt.update(core='72062939239b0f07707406309305a056a23f925f',
               toad='3b019be07ddcde7201a4a3332f7083bc127ee39b', native_package=str(package))
for distribution, key in [('agent-comms', 'core'), ('batrachian-toad', 'toad'), ('textual', 'textual')]:
    provenance = json.loads(metadata.distribution(distribution).read_text('direct_url.json'))
    assert provenance['vcs_info']['commit_id'] == receipt[key]
    assert not provenance.get('dir_info', {}).get('editable', False)
links = {name: Path('/home/ts/.local/bin') / name for name in receipt['launcher_names']}
for name, link in links.items():
    assert link.is_symlink() and link.resolve() == (old / 'bin' / name).resolve(), name
    assert (new / 'bin' / name).is_file(), name

# Explicit reviewed launch pins apply only to this operator and its new owners.
os.environ.update(AGENT_COMMS_ROOT=str(previous.root))
os.environ[ROOT_ID_ENV] = previous.wire_root_id
os.environ[PACKAGE_ENV] = str(package)
comms = wire()
selections = []
with _store_lock(comms._wire_lock_path):
    snapshot = comms.registry.snapshot()
    for owner in snapshot.threads.values():
        if not (owner.role.executable and snapshot.statuses[owner.name].active and owner.process_alive):
            continue
        owner.require_idle()
        selection = OwnerRestartSelection.capture(snapshot, owner.name)
        raw = Path(f'/proc/{owner.pid}/environ').read_bytes()
        environment = {os.fsdecode(k): os.fsdecode(v) for k, v in
                       (entry.split(b'=', 1) for entry in raw.split(b'\0') if b'=' in entry)}
        # An environment name is a route: resolve renamed owners through the registry.
        assert snapshot.require_active(environment['AGENT_COMMS_THREAD']).process_identity == selection.process
        assert Path(environment['AGENT_COMMS_AGENT_BIN']).name == 'pi-comms-native'
        args = shlex.split(environment['AGENT_COMMS_AGENT_ARGS']) if 'AGENT_COMMS_AGENT_ARGS' in environment else None
        selections.append((owner, selection, environment, args))

attempt = {'state': 'preflighted', 'activation': receipt, 'restarts': []}
receipt_path.write_text(json.dumps(attempt, indent=2) + '\n')
for original, selection, environment, args in selections:
    attempt['in_progress'] = original.name
    receipt_path.write_text(json.dumps(attempt, indent=2) + '\n')
    result, = comms.owners.restart_owners([original.name], agent_bin=str(new / 'bin/pi-comms-native'),
                                        agent_args=args, expected=selection, environment=environment)
    updated = comms.registry.require(original.name)
    assert (updated.session_file, updated.model, updated.thinking_level, updated.tags, updated.goal) == (
            original.session_file, original.model, original.thinking_level, original.tags, original.goal)
    attempt['restarts'].append({'name': original.name, 'previous_pid': result.previous_pid, 'pid': result.pid,
                                'settings_preserved': True})
    attempt.pop('in_progress')
    receipt_path.write_text(json.dumps(attempt, indent=2) + '\n')
    print(json.dumps(attempt['restarts'][-1]), flush=True)

(new / 'activation.json').write_text(json.dumps(receipt, indent=2) + '\n')
path = active_route_path()
directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
try:
    fcntl.flock(directory, fcntl.LOCK_EX)
    _publish_active_route_locked(replace(previous, native_package=package), path, directory, expected=previous)
    for name, link in links.items():
        temporary = link.with_name(link.name + '.paired-checkpoint-activation')
        assert not temporary.exists() and not temporary.is_symlink()
        temporary.symlink_to(new / 'bin' / name)
        os.replace(temporary, link)
finally:
    os.close(directory)
attempt['state'] = 'activated'
receipt_path.write_text(json.dumps(attempt, indent=2) + '\n')
print('PAIRED_DEFAULT_ACTIVATED', flush=True)
