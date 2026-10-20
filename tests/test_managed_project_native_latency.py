"""Installed owner IPC/native over original retained history; provider only is controlled."""
from agent_comms.pi_vocabulary import OffThinkingLevel
from agent_comms.native_package import pinned_native_package

import asyncio
import functools
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import time
from dataclasses import replace

from acp.schema import TextContentBlock
import pytest

from agent_comms.acp import CommsClient
from agent_comms.acp_extension import InputFailedUpdate, RequestFailedUpdate, decode_updates
from agent_comms.comms import Comms
from agent_comms.input_drain import InputDrain
from agent_comms.native_pi import NativePiRpcLaunch
from agent_comms.owned_turn import OwnedTurn
from agent_comms.runtime_requests import ProjectRuntimeRequest
from agent_comms.pi_native_backend import PiNativeBackend
import agent_comms.native_package as native_package
from delivery_owner_fixture import canonical_agent


