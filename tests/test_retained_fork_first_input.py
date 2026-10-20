"""Opt-in current installed fork check using an actual retained source, localhost only."""
from agent_comms.pi_vocabulary import OffThinkingLevel

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest

from agent_comms.acp import CommsClient
from agent_comms.acp_extension import InputFailedUpdate, RequestFailedUpdate, decode_updates
from agent_comms.comms import Comms
from agent_comms.input_attempt import StartedInput
from agent_comms.input_disposition import InputDispositions
from agent_comms.thread_management import ForkSpec
from agent_comms.threads import Thread
from stored_inputs import stored_inputs


