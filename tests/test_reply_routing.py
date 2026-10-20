from bus_history import read_history
from agent_comms.channel_targets import CommandTarget
import asyncio
import json

import pytest

import agent_comms.agent_events as ae
from agent_comms.tools import invoke_tool
from agent_comms.acp_extension import QueuePromptRequest, encode_request
from agent_comms.input_origin import HumanInputOrigin
from delivery_owner_fixture import canonical_agent
from agent_comms.comms import wire
from agent_comms.routing import MessageRoute
from agent_comms.threads import Thread


def test_incoming_route_distinguishes_channel_and_direct_delivery():
    assert MessageRoute("user", ("#experiment",)).incoming_scope == "#experiment"
    assert MessageRoute("user", ("worker",)).incoming_scope == "direct message"


