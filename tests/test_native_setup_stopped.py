"""Setup refusal stops a real copied-session ACP worker before selecting its DM."""
import asyncio
import json
import shutil
from pathlib import Path

import pytest

from agent_comms.activity import StoppedDrainDiagnostic
from agent_comms.coordinated_runtime import SelectedExecution


