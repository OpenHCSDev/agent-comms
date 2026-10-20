"""The committed native start event, not a connector observation, wakes held work."""

import os
import subprocess
from dataclasses import replace

import pytest

from agent_comms.child_process import ProcessIdentity
from agent_comms.store_files import _store_lock
from test_commander_heartbeat import external_commander
from test_commander_problem_orders import create


