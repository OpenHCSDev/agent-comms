"""Catalogs through actual pinned Pi, without a provider prompt or saved input."""
from agent_comms import native_package
from agent_comms.native_package import VerifiedNativePackage, pinned_native_package
from agent_comms.native_arguments import SelectedModel

import asyncio
import json
from dataclasses import replace

import pytest
from agent_comms.native_startup import NativeStartup
from agent_comms.native_session_prepare import NativeSessionPreparation
from agent_comms.pi_vocabulary import OffThinkingLevel

import agent_comms.backend as backend
from agent_comms.child_process import AttachedChild, BoundedRun
from agent_comms.comms import Comms
from agent_comms.config_options import ModelConfigOption, ThinkingLevelConfigOption
from agent_comms.native_pi import NativePiRpcLaunch, NativePiUnavailable
from agent_comms.pi_commands import SetModel, SetThinkingLevel
from agent_comms.pi_events import Response
from agent_comms.pi_native_backend import PersistentPiSession
from agent_comms.pi_rpc import PiRpcChannel
from agent_comms.threads import Thread
from delivery_owner_fixture import canonical_agent


@pytest.mark.refactor_guard
def test_backend_no_longer_owns_discovery_or_catalog_projection():
    assert not {"discover_models", "discover_thinking_levels", "Model"}.intersection(vars(backend))


