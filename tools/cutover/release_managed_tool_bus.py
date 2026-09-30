"""One-use same-format pair publication through the existing retained batch.

Original launch credentials stay in the existing RAM-only handoff. This tool
owns no turn, input, goal, message, or journal state and never retries an input.
"""
from dataclasses import dataclass, fields
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import selectors
import signal
import sys
import time

from agent_comms.active_route import (
    ActiveRoute, active_route_path, read_active_route, _publish_active_route_locked,
)
from agent_comms.comms import Comms
from agent_comms.child_process import ProcessIdentity, Platform, PidfdHandles
from agent_comms.field_codec import FieldCodec
from agent_comms.owner_launch import RestartEnvironment
from agent_comms.store_files import _atomic_write_text
from agent_comms.native_package import verify_native_package

RESTORE_TOOLS = Path('/home/ts/wt/comms-original-thinking-restore-20260930/tools/cutover')
sys.path.insert(0, str(RESTORE_TOOLS))
from original_thinking_restore import RestoreOriginalThinking, prepare_off_restoration

SOURCE = Path('/home/ts/.local/share/agent-comms/runtime-owner-cli-config-20260930')
CURRENT = Path('/home/ts/.local/share/agent-comms/runtime-viewport-buffer-20260930')
TARGET = Path('/home/ts/.local/share/agent-comms/runtime-managed-tool-bus-custody-20260930')
NATIVE = Path('/home/ts/.local/share/agent-comms/native-current-ceca2c05cf0ae07b/node_modules/@earendil-works/pi-coding-agent')
EXTENSION = Path('/home/ts/.pi/agent/extensions/agent-comms/index.ts')
EXTENSION_SOURCE = Path('/home/ts/wt/comms428-native-wait-custody-20260930/extensions/pi-agent-comms/index.ts')
OLD_EXTENSION = 'bf8a6a4570a641c8a5e94aefaf1cb2d6958a015c4d399c36becc097bfd19e0ac'
NEW_EXTENSION = '40a4a85d790a5e9a156c7a412ce6f23da8b215ca7f8168e5468dff888a7bcb87'
COMMANDS = ('agent-comms', 'agent-comms-acp', 'agent-comms-agent', 'agent-comms-nk-foreground', 'toad')
LINKS = Path('/home/ts/.local/bin')
REVIEWED = dict.fromkeys(('agent-comms-ux', 'nra-architecture', 'openhcs-architecture-memory',
                        'openhcs-pr159-viewer-bind-owner', 'openhcs-helper', 'comms428'), 'medium')
REVIEWED.update(dict.fromkeys(('refactor-r1', 'nominal-refactor-advisor-2', 'openhcs-helper2'), 'low'))
PREIMAGE = Path('/home/ts/wt/comms-cleanup-live-integration-20260929/.release-private/c3-reviewed-pair-20260930/cutover.json.originals/registry.json')
PREIMAGE_SHA = '976b3797e42d6c9edb2bfd41139e0cca58bc36c3680adf492182f1b3ba758983'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass(frozen=True)
class PublishManagedToolBus(RestoreOriginalThinking):
    original_route: ActiveRoute
    target_route: ActiveRoute
    route_directory: int
    receipt: Path

    def note(self, phase, **facts):
        previous = json.loads(self.receipt.read_text())
        previous.update(phase=phase, **facts)
        _atomic_write_text(self.receipt, json.dumps(previous, indent=2) + '\n', fsync_parent=True)

    def require_selection(self, snapshot, owners):
        super().require_selection(snapshot, owners)
        if read_active_route() != self.original_route:
            raise RuntimeError('Reviewed original route changed')
        if digest(EXTENSION) != OLD_EXTENSION or digest(EXTENSION_SOURCE) != NEW_EXTENSION:
            raise RuntimeError('Reviewed extension preimage/source changed')
        for command in COMMANDS:
            if (LINKS / command).readlink() != CURRENT / 'bin' / command:
                raise RuntimeError('Reviewed default runtime changed')
            if not (TARGET / 'bin' / command).is_file():
                raise RuntimeError('Target entrypoint missing')

    def after_stopped(self, lifecycle):
        # The original lifecycle has retired EVERY captured exact process and
        # holds the wire exclusion. It retains and launches the same handoff.
        super().after_stopped(lifecycle)
        self.note('all-original-owners-stopped-and-original-thinking-restored')
        if digest(EXTENSION) != OLD_EXTENSION or digest(EXTENSION_SOURCE) != NEW_EXTENSION:
            raise RuntimeError('Extension changed after stop; batch remains stopped')
        mode = stat.S_IMODE(EXTENSION.stat().st_mode)
        _atomic_write_text(EXTENSION, EXTENSION_SOURCE.read_text(), fsync_parent=True)
        EXTENSION.chmod(mode)
        if digest(EXTENSION) != NEW_EXTENSION:
            raise RuntimeError('Published extension differs from reviewed source')
        _publish_active_route_locked(self.target_route, active_route_path(),
                                     self.route_directory, expected=self.original_route)
        for command in COMMANDS:
            link = LINKS / command
            if link.readlink() != CURRENT / 'bin' / command:
                raise RuntimeError('Default changed during quiet publication')
            temporary = LINKS / (command + '.managed-tool-bus-publish')
            if temporary.exists() or temporary.is_symlink():
                raise RuntimeError('Unreviewed prior publication temporary exists')
            temporary.symlink_to(TARGET / 'bin' / command)
            temporary.replace(link)
        self.note('paired-native-route-extension-and-five-defaults-published')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--ui-pid', type=int)
    parser.add_argument('--ui-birth', type=int)
    parser.add_argument('--receipt', type=Path, required=True)
    args = parser.parse_args()
    if args.receipt.exists():
        raise RuntimeError('Existing attempt receipt requires disposition, not automatic replay')
    activation = json.loads((TARGET / 'activation.json').read_text())
    if digest(TARGET / 'staging-receipt.json') != activation['staging_receipt_sha256']:
        raise RuntimeError('Activation and reviewed stage receipt differ')
    if activation['stage'] != str(TARGET) or activation['native_package'] != str(NATIVE):
        raise RuntimeError('Wrong staged runtime/native selection')
    verify_native_package(NATIVE)
    original = read_active_route()
    if original is None or str(original.root) != '/var/tmp/agent-comms-live-20260927-wzjtqhza':
        raise RuntimeError('Original private route differs')
    target = ActiveRoute(original.root, original.wire_root_id, NATIVE)
    service = Comms(original.root)
    service.owners.pin_private_nk_launch(target.root, target.wire_root_id, target.native_package)
    restore = prepare_off_restoration(service, original_python=Path('/home/ts/.local/share/agent-comms/runtime-canonical-source-publication-20260930/bin/python'),
        preimage=PREIMAGE, expected_sha256=PREIMAGE_SHA, reviewed=REVIEWED)
    snapshot = service.registry.snapshot()
    owners = [snapshot.threads[s.name] for s in restore.audience]
    for thread in owners:
        thread.require_idle()
    directory = os.open(active_route_path().parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(directory, fcntl.LOCK_EX | fcntl.LOCK_NB)
        operation = PublishManagedToolBus(
            **{f.name: getattr(restore, f.name) for f in fields(restore)},
            original_route=original, target_route=target,
            route_directory=directory, receipt=args.receipt)
        operation.require_selection(snapshot, owners)
        print(json.dumps({'state': 'reviewed-quiet-preflight', 'owners': len(owners),
                          'execute': args.execute}), flush=True)
        if not args.execute:
            return
        args.receipt.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _atomic_write_text(args.receipt, json.dumps({'phase': 'preflight-complete',
            'started': time.time(), 'target': str(TARGET), 'native': str(NATIVE),
            'owners_before': [FieldCodec.encode(s) for s in restore.audience]}, indent=2), fsync_parent=True)
        if args.ui_pid is None or args.ui_birth is None:
            raise RuntimeError('Exact original user UI identity is required before owner cutover')
        ui = ProcessIdentity(args.ui_pid, args.ui_birth)
        Platform.current().require(ui)
        command = Path(f'/proc/{ui.pid}/cmdline').read_bytes().split(b'\0', 1)[0]
        if command.rsplit(b'/', 1)[-1] != b'toad':
            raise RuntimeError('Reviewed user UI command changed')
        descriptor = PidfdHandles.open_pidfd(ui.pid)
        try:
            Platform.current().require(ui)
            PidfdHandles.signal_pidfd(descriptor, signal.SIGINT)
            with selectors.DefaultSelector() as selected:
                selected.register(descriptor, selectors.EVENT_READ)
                if not selected.select(10):
                    raise RuntimeError('Original user UI stop unconfirmed; owners untouched')
        finally:
            os.close(descriptor)
        operation.note('exact-original-user-ui-retired', ui=FieldCodec.encode(ui))
        environment = dict(os.environ)
        environment.pop('PYTHONPATH', None)
        environment.update(VIRTUAL_ENV=str(TARGET), PATH=str(TARGET / 'bin') + ':' + environment['PATH'])
        results = service.owners.restart_owners(
            runtime=RestartEnvironment.inherit(environment),
            source_interpreter=str(SOURCE / 'bin/python'), cutover=operation)
        after = service.registry.snapshot()
        for result in results:
            thread = after.threads[result.thread]
            process = thread.require_process()
            after.require_owner_process(after.owner_identity(thread.name), process)
            executable = Path(f'/proc/{process.pid}/cmdline').read_bytes().split(b'\0', 1)[0].decode()
            if executable != str(TARGET / 'bin/python'):
                raise RuntimeError('Retained owner launched another interpreter')
        operation.note('paired-owners-process-verified-ui-acceptance-pending',
            finished=time.time(), results=FieldCodec.encode(results))
        print(json.dumps({'state': 'paired-retained-owners-launched', 'owners': len(results)}), flush=True)
    finally:
        os.close(directory)


if __name__ == '__main__':
    main()
