"""Decode actual send exceptions once into the shared delivery failure family."""

from .acp_extension import ClaimBlockedFailure, BackendDeliveryFailure, UnknownDeliveryFailure
from .errors import HumanClaimBlockedError, HumanInitialUnknownError
from .mro_dispatch import MroDispatch, handles


class FailureDecoder(MroDispatch):
    def __init__(self):
        self.failure = None

    @handles(HumanInitialUnknownError)
    def unknown(self, error):
        self.failure = UnknownDeliveryFailure(str(error), error.wire_root_id, error.wire_seq, error.message_id)

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
