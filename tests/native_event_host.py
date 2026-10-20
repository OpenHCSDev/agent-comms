"""Opt-in local SDK fixture using the same typed native launch boundary."""

import json
import sys
from dataclasses import replace
from pathlib import Path

from agent_comms.native_pi import NativePiRpcLaunch


