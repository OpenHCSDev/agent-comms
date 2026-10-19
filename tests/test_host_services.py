"""An install restarts every declared service running on its host, and only those."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Self

import pytest

from agent_comms.host_services import HostService, ServiceNotServingError
from agent_comms.host_services.connector import ConnectorService

INSTALLED = Path('/stack/.venv/bin/python')
INSTALLATION = INSTALLED.parents[2]


@pytest.fixture
def host(monkeypatch):
    """A service this test declares, run by an in-memory host."""

    @dataclass(frozen=True)
    class ProbeService(HostService):
        generation: int
        instances: ClassVar[list[ProbeService]] = []
        restarted_runtime: ClassVar[Path] = INSTALLED
        comes_back: ClassVar[bool] = True

        @classmethod
        def running(cls) -> tuple[Self, ...]:
            return tuple(cls.instances)

        def restart(self) -> None:
            type(self).instances = [type(self)(self.generation + 1)] * self.comes_back

        def runtime(self) -> Path:
            return INSTALLED if self.generation == 0 else self.restarted_runtime

    monkeypatch.setattr('time.sleep', lambda seconds: None)
    monkeypatch.setattr(ConnectorService, 'running', classmethod(lambda cls: ()))
    yield ProbeService
    del HostService.__registry__[ProbeService.declared_name]


def test_only_running_services_restart_and_each_must_serve_the_installed_runtime(host):
    assert ConnectorService.running() == ()
    assert HostService.restart_running(INSTALLED, INSTALLATION) == ()

    host.instances = [host(0)]
    (report,) = HostService.restart_running(INSTALLED, INSTALLATION)
    assert host.instances == [host(1)] and str(INSTALLED) in report

    host.instances, host.restarted_runtime = [host(0)], Path('/old/bin/python')
    with pytest.raises(ServiceNotServingError, match='not the installed'):
        HostService.restart_running(INSTALLED, INSTALLATION)

    host.instances, host.comes_back = [host(0)], False
    with pytest.raises(ServiceNotServingError, match='did not serve again'):
        HostService.restart_running(INSTALLED, INSTALLATION)


def test_the_connector_is_a_declared_service():
    assert ConnectorService in HostService.members_with(HostService)
