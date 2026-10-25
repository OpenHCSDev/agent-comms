"""Session ownership, declaration extension and actual ACP boundary contracts."""


from agent_comms.repo import Checkout
import pytest

from agent_comms.errors import RelationViolationError
from agent_comms.threads import Thread


def test_thread_service_tier_round_trips_legacy_rows(*, thread_registration):
    from agent_comms.field_codec import FieldCodec
    from agent_comms.pi_vocabulary import ServiceTier

    encoded = FieldCodec.encode(thread_registration.declare(Thread('legacy', frozenset(), Checkout.from_wire('.'))))
    assert encoded.pop("service_tier") == "priority"
    restored = FieldCodec.decode(Thread, encoded)
    assert restored.service_tier is ServiceTier.PRIORITY
    standard = thread_registration.declare(Thread('standard', frozenset(), Checkout.from_wire('.'), service_tier=ServiceTier.STANDARD))
    assert FieldCodec.decode(Thread, FieldCodec.encode(standard)).service_tier is ServiceTier.STANDARD
    with pytest.raises(RelationViolationError):
        standard.require_original_model_configuration(restored)


