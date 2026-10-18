from agent_comms.acp_extension import BackendDeliveryFailure, AdmissionBlockedFailure, UnknownDeliveryFailure, InputFailedUpdate, encode_updates, decode_updates
from agent_comms.delivery_failure import delivery_failure
from agent_comms.errors import HumanInitialUnknownError, HumanAdmissionBlockedError


def test_delivery_family_decodes_once_and_preserves_unknown_identity():
    errors = (OSError('refused'), HumanInitialUnknownError('root', 17, 'input'), HumanAdmissionBlockedError('reserved gap'))
    expected = (BackendDeliveryFailure, UnknownDeliveryFailure, AdmissionBlockedFailure)
    for error, member in zip(errors, expected):
        value = delivery_failure(error)
        assert type(value) is member
        assert value.description == str(error)
        assert decode_updates(encode_updates(InputFailedUpdate('body', value))) == (InputFailedUpdate('body', value),)
    unknown = delivery_failure(errors[1])
    assert (unknown.wire_root_id, unknown.wire_seq, unknown.message_id) == ('root', 17, 'input')
