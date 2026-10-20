"""The published route is the default; explicit roots win and environments do not."""
import json
import os
import subprocess
import sys
from dataclasses import fields
from pathlib import Path

import pytest

from agent_comms.active_route import ActiveRoute, resolve_comms_route
from agent_comms.cli import main
from agent_comms.comms import Comms
from agent_comms.errors import RelationViolationError
from agent_comms.launch_root import LaunchRoot
from agent_comms.launch_runtime import LaunchRuntime, RestartEnvironment, RestoredRuntime
from agent_comms.route_selection import RouteSelection, child_root


