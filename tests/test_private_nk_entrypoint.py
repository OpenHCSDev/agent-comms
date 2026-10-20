"""Private N/K launch authority for ACP, worker and CLI owners; no provider call."""

from __future__ import annotations

from agent_comms.native_package import VerifiedNativePackage, pinned_native_package
from conftest import publish_initial
import json
import fcntl
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import agent_comms.acp as acp
import agent_comms.active_route as active_route
import agent_comms.cli as cli
import agent_comms.private_nk_entrypoint as private_nk_entrypoint
import agent_comms.worker as worker
from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.child_process import ParentedProcess, ProcessIdentity
from agent_comms.comms import Comms, wire
from agent_comms.coordination_cohort import accept_delivery_cohort
from agent_comms.coordination_errors import IdentityConflict, PublicationActivationBlocked
from agent_comms.coordinator import Coordination
from agent_comms.private_nk_entrypoint import PrivateNkLaunch, RootLaunch, SharedRootLaunch
from agent_comms.threads import Thread
from test_native_prompt_binding import _root
from test_native_prompt_binding import tmp_path as private_root_fixture

tmp_path = private_root_fixture


