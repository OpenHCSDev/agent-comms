"""Decode actual send exceptions once into the shared delivery failure family."""

from .acp_extension import AdmissionBlockedFailure, BackendDeliveryFailure, UnknownDeliveryFailure
from .errors import HumanAdmissionBlockedError, HumanInitialUnknownError
from .mro_dispatch import MroDispatch, handles


class FailureDecoder(MroDispatch):
    def __init__(self):
        self.failure = None

    @handles(HumanInitialUnknownError)
    def unknown(self, error):
        self.failure = UnknownDeliveryFailure(str(error), error.wire_root_id, error.wire_seq, error.message_id)

    @handles(HumanAdmissionBlockedError)
    def blocked(self, error):
        self.failure = AdmissionBlockedFailure(str(error))

    @handles(Exception)
    def refused(self, error):
        if self.failure is None:
            self.failure = BackendDeliveryFailure(str(error))


def delivery_failure(error: Exception):
    decoder = FailureDecoder()
    decoder.dispatch_sync(error)
    return decoder.failure
