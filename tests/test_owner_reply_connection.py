"""Owner reads bound the dispatched reply, not local connection preparation."""

import asyncio
import json
import os

from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.runtime import OwnerReplyConnection
from agent_comms.threads import Thread
from agent_comms.ui_model.observation import OwnerRead, ReadAnswered, ReadRefused


class GoalRead(OwnerRead):
    async def read(self, owner):
        return await owner.request("goal_snapshot")


class Writer:
    def __init__(self):
        self.written = bytearray()

    def write(self, data):
        self.written.extend(data)

    async def drain(self):
        pass

    def close(self):
        pass

    async def wait_closed(self):
        pass


