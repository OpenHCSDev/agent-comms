"""The native pin matches this checkout's tree before any native package is built."""

from pathlib import Path

import pytest

from agent_comms.native_build_inputs import NativeBuildInputs

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def _native_runtime_resources_are_explicit():
    """This module reads the source pin, so it acquires no built native package."""


def test_native_pin_matches_tree():
    NativeBuildInputs(ROOT).require_pinned(ROOT / "stack/pi-native.sha256")
