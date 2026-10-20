"""An install restarts every declared service running on its host, and only those."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Self

import pytest

from agent_comms.host_services import HostService, ServiceNotServingError
from agent_comms.host_services.connector import ConnectorService

INSTALLED = Path('/stack/.venv/bin/python')


def test_the_connector_is_a_declared_service():
    assert ConnectorService in HostService.members_with(HostService)
