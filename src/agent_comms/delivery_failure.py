"""Decode actual send exceptions once into the shared delivery failure family."""

from agent_comms.acp_extension import ClaimBlockedFailure, BackendDeliveryFailure
from agent_comms.errors import HumanClaimBlockedError
from .mro_dispatch import MroDispatch, handles


class FailureDecoder(MroDispatch):
    def __init__(self):
        self.failure = None

    @handles(HumanClaimBlockedError)
    def blocked(self, error):
        self.failure = ClaimBlockedFailure(str(error))

    @handles(Exception)
    def refused(self, error):
        if self.failure is None:
            self.failure = BackendDeliveryFailure(str(error))


def delivery_failure(error: Exception):
    decoder = FailureDecoder()
    decoder.dispatch_sync(error)
    return decoder.failure
