"""The claude.ai connector, when this host serves it."""

from . import SystemdUserService


class ConnectorService(SystemdUserService):
    unit = "agent-comms-connector.service"
