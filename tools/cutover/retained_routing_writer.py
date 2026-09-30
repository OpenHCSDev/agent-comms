"""Original old720 decoder and certificate retained through the full carry."""
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys

from agent_comms.comms import Comms
from agent_comms.field_codec import FieldCodec
from agent_comms.store_files import _store_lock
from routing_carry import prepare_original
from routing_recovery import retain_originals


def main():
    root, target_python, installer, root_id, receipt = sys.argv[1:]
    service = Comms(Path(root))
    environment = dict(os.environ)
    environment.pop('PYTHONPATH', None)
    with (service.bus.log.locked() as custody,
          _store_lock(service.root / 'registry.json'),
          _store_lock(service.root / 'transcript_routes.sqlite3')):
        marker = service.bus.log._private_marker_unlocked()
        if marker.root_id != root_id or marker.checkpoint_seal is None:
            raise ValueError('Original certified writer identity changed.')
        plan = prepare_original(service, marker)
        encoded = json.dumps(FieldCodec.encode(plan))
        subprocess.run([
            target_python, str(Path(installer).with_name('validate_retained_routing.py')),
            root, str(custody), root_id,
        ], input=encoded, text=True, env=environment, pass_fds=(custody,), check=True)
        retain_originals(service, Path(receipt), plan)
        # Every original and target routing cell has now been validated. This is
        # the same old-writer/index operation, not a second batch or lock path.
        retained = replace(marker, checkpoint_version=None, checkpoint_seal=None)
        service.bus.log.write_metadata_unlocked(retained)
        (service.root / 'private_bus_checkpoint.sqlite3').unlink()
        subprocess.run([target_python, installer, root, str(custody), root_id, receipt],
                       input=encoded, text=True, env=environment,
                       pass_fds=(custody,), check=True)
        after = service.bus.log.read_metadata_unlocked()
        if replace(after, checkpoint_version=None, checkpoint_seal=None) != retained:
            raise ValueError('Carry changed original wire admission/identity metadata.')
        plan.require_sources(service.root)


if __name__ == '__main__':
    main()
