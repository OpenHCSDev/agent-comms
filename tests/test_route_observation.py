"""Route observation shares private marker validation without service/store reads."""

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_comms.active_route import ActiveRoute, CommsRoute, LocalRoute, read_active_route, resolve_comms_route
from agent_comms.launch_root import LaunchRoot
from agent_comms.comms import Comms, wire
from agent_comms.errors import RelationViolationError
import agent_comms.private_nk_entrypoint as private_nk_entrypoint
from agent_comms.private_nk_entrypoint import PrivateNkLaunch
from agent_comms.registration import Registration
from agent_comms.registry_document import RegistryDocument
from agent_comms.wire_log import WireLog


