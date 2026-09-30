"""One authorized original-format batch to target-format/default-route cutover.

Run from the target runtime. The authentic original interpreter alone admits
and retires original owners. Never run this against the public root without the
parent's release authorization. No credential is written or passed in argv.

Admission requirement: the release owner must first select the target default
launchers and gracefully retire ALL original UI/ACP registry writers. Keep
original-client ingress excluded until this operation returns. Maintenance
admission fences executable owners; it does not revoke already imported client
code or prevent its metadata/history calls from rewriting retired Thread fields.
This operator does not discover or retire that external client audience. See
docs/checkpoints/old-thread-client-exclusion-20260930.md and its actual writer
proof. An executable-owner batch alone is not sufficient release admission.
"""
import argparse
from dataclasses import replace
import fcntl
import json
import os
from pathlib import Path
import sys

from agent_comms.active_route import read_active_route
from agent_comms.field_codec import FieldCodec
from agent_comms.native_pi import _trusted_package
from agent_comms.owner_launch import RestartEnvironment
import agent_comms.owner_restart
from cutover_child import run_cutover_child


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--original-python', type=Path, required=True)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--root-id', required=True)
    parser.add_argument('--native-package', type=Path, required=True)
    parser.add_argument('--route-path', type=Path, required=True)
    parser.add_argument('--receipt', type=Path, required=True)
    arguments = parser.parse_args()
    # The publisher uses this SAME native trust boundary. Validate it before
    # any fence/signal, without asking a target registry reader to read old data.
    _trusted_package(arguments.native_package)
    descriptor = os.open(arguments.route_path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        # Canonical directory -> wire lock order, held across the WHOLE batch.
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        original = read_active_route(arguments.route_path)
        if original is None or original.root != arguments.root or original.wire_root_id != arguments.root_id:
            raise ValueError('Original active route does not name the requested root')
        original.observe_root()
        target = replace(original, native_package=arguments.native_package)
        runtime = Path(sys.executable).parent
        environment = dict(os.environ, AGENT_COMMS_ROOT=str(target.root),
                           AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID=target.wire_root_id,
                           AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE=str(target.native_package),
                           VIRTUAL_ENV=str(runtime.parent),
                           PATH=str(runtime) + os.pathsep + os.environ.get('PATH', ''))
        packet = {'target_python': sys.executable, 'target_binary': str(runtime / 'pi-comms-native'),
                  'target_environment': environment,
                  'runtime': FieldCodec.encode(RestartEnvironment.inherit(environment)),
                  'original_route': FieldCodec.encode(original), 'target_route': FieldCodec.encode(target),
                  'route_path': str(arguments.route_path), 'receipt': str(arguments.receipt)}
        authentic = dict(os.environ)
        authentic.pop('PYTHONPATH', None)
        authentic.update(AGENT_COMMS_ROOT=str(original.root),
                         AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID=original.wire_root_id,
                         AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE=str(original.native_package))
        result = run_cutover_child([
            str(arguments.original_python),
            str(Path(__file__).with_name('restart_original_thread_format.py')),
            str(Path(agent_comms.owner_restart.__file__).parent), str(descriptor),
        ], packet=json.dumps(packet), environment=authentic, descriptors=(descriptor,))
        print(result.stdout.strip(), flush=True)
    finally:
        os.close(descriptor)


if __name__ == '__main__':
    main()
