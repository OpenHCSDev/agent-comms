"""The connector's readiness witness comes from its real listening socket."""
import asyncio
import socket

import pytest
from aiohttp import web

from agent_comms.host_services import BoundListener, ServiceNotServingError, SystemdUserService
from connector_readiness import ConnectorReadiness


def test_readiness_requires_the_started_sites_listening_socket(monkeypatch):
    async def check():
        runner = web.AppRunner(web.Application())
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        try:
            with pytest.raises(TypeError):
                BoundListener(site)
            with pytest.raises(ServiceNotServingError):
                BoundListener.of(site)
            with pytest.raises(TypeError):
                SystemdUserService.ready()
            await site.start()
            listener = BoundListener.of(site)
            assert listener.site is site
            notification = ConnectorReadiness()
            try:
                monkeypatch.setenv(SystemdUserService.notify_variable, "@" + notification.name)
                SystemdUserService.ready(listener)
                assert notification.socket.recv(4096) == b"READY=1"
                with socket.socket() as connected:
                    connected.connect(("127.0.0.1", listener.site.port))
            finally:
                notification.close()
        finally:
            await runner.cleanup()
        with pytest.raises(ServiceNotServingError):
            BoundListener.of(site)

    asyncio.run(check())
