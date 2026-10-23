"""Session ownership, declaration extension and actual ACP boundary contracts."""


import pytest

from agent_comms.errors import RelationViolationError
from agent_comms.threads import Thread


def test_thread_service_tier_round_trips_legacy_rows(*, thread_registration):
    from agent_comms.field_codec import FieldCodec
    from agent_comms.pi_vocabulary import ServiceTier

    encoded = FieldCodec.encode(Thread('legacy', frozenset(), '.'))
    assert encoded.pop("service_tier") == "priority"
    restored = FieldCodec.decode(Thread, encoded)
    assert restored.service_tier is ServiceTier.PRIORITY
    standard = Thread('standard', frozenset(), '.', service_tier=ServiceTier.STANDARD)
    assert FieldCodec.decode(Thread, FieldCodec.encode(standard)).service_tier is ServiceTier.STANDARD
    with pytest.raises(RelationViolationError):
        standard.require_original_model_configuration(restored)


