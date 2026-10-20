"""Actual saved Pi -> native extension UI -> owner ACP socket -> same Pi reply."""

import asyncio
import json
from types import SimpleNamespace

from agent_comms.acp_extension import McpClientAcknowledgementUpdate, decode_updates
from agent_comms.comms import Comms
from agent_comms.runtime import RuntimeProxy
from delivery_owner_fixture import canonical_agent
from native_event_host import install_event_host


