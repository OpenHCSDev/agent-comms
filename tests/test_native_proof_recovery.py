"""Saved, actual pinned native sessions with indexed proof; loopback provider only."""

import asyncio
import json
import os
import sqlite3
import subprocess
import time
from contextlib import closing
from pathlib import Path
from typing import get_args, get_type_hints

import pytest

from agent_comms.native_pi import NativeContextJournal, NativeContextProof, NativePiUnavailable
from agent_comms.native_session_prepare import NativeSessionPreparation
from agent_comms.pi_commands import GetState, Prompt
from agent_comms.native_tool_call import OwnerToolConnection
from agent_comms.pi_events import ContextCommitted, InputCommitted, Response
from native_proof_cases import read_proof_rows, write_proof_rows


