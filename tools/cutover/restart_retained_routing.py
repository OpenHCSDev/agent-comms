"""Authorized operator entrypoint: one existing all-owner retained batch."""
import argparse
import json
import os
from pathlib import Path

from agent_comms.comms import Comms
from agent_comms.field_codec import FieldCodec
from agent_comms.private_nk_entrypoint import PACKAGE_ENV, ROOT_ID_ENV
from retained_routing_cutover import RetainedRoutingCutover


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--original-python', type=Path, required=True)
    parser.add_argument('--root-id', required=True)
    parser.add_argument('--native-package', type=Path, required=True)
    parser.add_argument('--receipt', type=Path, required=True)
    arguments = parser.parse_args()
    os.environ.update(AGENT_COMMS_ROOT=str(arguments.root),
                      **{ROOT_ID_ENV: arguments.root_id, PACKAGE_ENV: str(arguments.native_package)})
    operation = RetainedRoutingCutover(arguments.original_python, arguments.root_id,
                                       arguments.native_package, arguments.receipt)
    results = Comms(arguments.root).owners.restart_owners(cutover=operation)
    print(json.dumps(FieldCodec.encode(results)), flush=True)


if __name__ == '__main__':
    main()
