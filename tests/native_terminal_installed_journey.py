"""One new configured-provider ACP input, one local effect and one final reply.

Run against an isolated installed wheel with an explicit private root/package.
Never imports or replays a prior saved input. The caller supplies an isolated
copy of provider configuration, including the native models-store.json;
the original configuration is never written.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
from unittest.mock import patch

from agent_comms import activity, runtime_updates
from agent_comms.launch_runtime import ThreadLaunch
from agent_comms.acp import CommsAgent
from agent_comms.comms import Comms
from agent_comms.threads import Thread
from agent_comms.wire_record import WireRecord


class Client:
    def __init__(self):
        self.updates = []

    async def session_update(self, session_id, update, **kwargs):
        self.updates.append(update.model_dump(by_alias=True, exclude_none=True))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--package', type=Path, required=True)
    parser.add_argument('--launcher', type=Path, required=True)
    parser.add_argument('--provider', required=True)
    parser.add_argument('--model', required=True)
    parser.add_argument('--thinking', required=True)
    parser.add_argument('--read-only', action='store_true')
    args = parser.parse_args()
    asyncio.run(main(args.output, args.package, args.launcher, args.provider, args.model, args.thinking, args.read_only))
