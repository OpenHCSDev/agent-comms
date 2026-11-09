"""Session ownership, declaration extension and actual ACP boundary contracts."""
from pathlib import Path

import pytest

from agent_comms.errors import RelationViolationError
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.threads import NativeThread, Thread


def test_thread_service_tier_defaults_standard_and_preserves_explicit_priority(*, thread_registration):
    from agent_comms.execution.pi_vocabulary import ServiceTier
    from agent_comms.nominal.field_codec import FieldCodec

    encoded = FieldCodec.encode(thread_registration.declare(NativeThread('legacy', frozenset(), Checkout(Path('.')))))
    assert encoded.pop("service_tier") == "standard"
    restored = FieldCodec.decode(Thread, encoded)
    assert restored.service_tier is ServiceTier.STANDARD
    priority = thread_registration.declare(NativeThread('priority', frozenset(), Checkout(Path('.')), service_tier=ServiceTier.PRIORITY))
    assert FieldCodec.decode(Thread, FieldCodec.encode(priority)).service_tier is ServiceTier.PRIORITY
    with pytest.raises(RelationViolationError):
        priority.require_original_model_configuration(restored)


