"""Ordinary ACP input commits a native write claim through the real publisher."""

import asyncio
import json
import os
import secrets
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from acp.schema import TextContentBlock

from agent_comms.message_page import MessagePageRequest
from agent_comms.envelope_claim_transitions import NativeClaim, ReleasedClaims, UnclaimedResource
from agent_comms.messages import ClaimHistoryKind, ClaimMessage, Message
from agent_comms.check_root import CheckRoot
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.coordination_session import NativeBoundary
from agent_comms.envelope_claim_transitions import WritableFileClaim
from delivery_owner_fixture import reply_turn_finished
from native_backend_fixture import NativeBackendFixture
from agent_comms.read_basis import AllChannelDisplayScope


