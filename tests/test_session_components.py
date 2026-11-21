"""Session ownership, declaration extension and actual ACP boundary contracts."""
from pathlib import Path

import pytest

from agent_comms.errors import RelationViolationError
from agent_comms.execution.pi_vocabulary import ServiceTier
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.thread_settings import ContextEditPolicy
from agent_comms.registry.threads import NativeThread, Thread


def test_thread_service_tier_defaults_standard_and_preserves_explicit_priority(*, thread_registration):

    encoded = FieldCodec.encode(thread_registration.declare(NativeThread('legacy', frozenset(), Checkout(Path('.')))))
    assert encoded.pop("service_tier") == "standard"
    assert encoded.pop("context_edit_policy") == "optional"
    restored = FieldCodec.decode(Thread, encoded)
    assert restored.service_tier is ServiceTier.STANDARD
    assert restored.context_edit_policy is ContextEditPolicy.OPTIONAL
    priority = thread_registration.declare(NativeThread('priority', frozenset(), Checkout(Path('.')), service_tier=ServiceTier.PRIORITY))
    assert FieldCodec.decode(Thread, FieldCodec.encode(priority)).service_tier is ServiceTier.PRIORITY
    required = thread_registration.declare(NativeThread(
        'required-context-edit', frozenset(), Checkout(Path('.')),
        context_edit_policy=ContextEditPolicy.REQUIRED_EACH_TURN,
    ))
    assert FieldCodec.decode(
        Thread, FieldCodec.encode(required)
    ).context_edit_policy is ContextEditPolicy.REQUIRED_EACH_TURN
    with pytest.raises(RelationViolationError):
        priority.require_original_model_configuration(restored)


