"""The existing stream and harness capabilities consumed by portable import."""

from agent_comms.agent_runtime import ReadsHistory
from agent_comms.installer.harness_install import HarnessSession


class ImportHistorySource(ReadsHistory, HarnessSession, declared_name=None):
    """An original harness session that can supply its retained transcript updates."""
