"""Installing Claude Code writes exactly the declared hook programs, once each."""
import json
import shlex
import tomllib
from importlib.metadata import EntryPoint, EntryPoints
from pathlib import Path
from types import SimpleNamespace

import pytest

import agent_comms_hooks
import agent_comms.harness_install as harness_install
from agent_comms.harness_install import ClaudeCodeHarness


