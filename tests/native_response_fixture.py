"""A real session owner for response policy checks, without acquiring a child."""

from pathlib import Path

import pytest

from agent_comms.launch_runtime import LaunchRuntime, RestartEnvironment
from agent_comms.native_pi import NativePiRpcLaunch
from agent_comms.native_session_prepare import NativeSessionPreparation
from agent_comms.native_startup import NativeStartup
from agent_comms.native_child import PersistentPiSession
from agent_comms.selected_session import SelectedSession


@pytest.fixture
def native_response_session(tmp_path: Path) -> NativeSessionPreparation:
    launch = NativePiRpcLaunch(
        ("unused",), tmp_path, {}, SelectedSession(tmp_path), tmp_path,
        root=tmp_path, runtime=LaunchRuntime.running(),
        configuration=RestartEnvironment.inherit({}),
    )
    return NativeSessionPreparation(
        launch, native_session=PersistentPiSession(), startup=NativeStartup(tmp_path),
    )
