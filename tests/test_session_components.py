"""Session ownership, declaration extension and actual ACP boundary contracts."""

import os
from dataclasses import replace

import pytest
from agent_comms.pi_vocabulary import NoThinkingOverride, OffThinkingLevel
from acp.agent.router import build_agent_router
from acp.schema import SessionConfigSelectOption

from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.acp import CommsClient
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.coordinator import Coordination
from agent_comms.errors import RelationViolationError
from agent_comms.config_options import (
    CatalogConfigOption,
    ConfigOption,
    ModelConfigOption,
    ThinkingLevelConfigOption,
)
from agent_comms.session_lifecycle import AttachedSessionLifecycle, SessionLifecycle
import agent_comms.agent_events as events
from agent_comms.pi_vocabulary import OffThinkingLevel, HighThinkingLevel
from delivery_owner_fixture import canonical_agent


def test_thread_service_tier_round_trips_legacy_rows(*, thread_registration):
    from agent_comms.field_codec import FieldCodec
    from agent_comms.pi_vocabulary import ServiceTier
    from agent_comms.threads import Thread

    encoded = FieldCodec.encode(thread_registration.new_thread('legacy', frozenset(), '.'))
    assert encoded.pop("service_tier") == "priority"
    restored = FieldCodec.decode(Thread, encoded)
    assert restored.service_tier is ServiceTier.PRIORITY
    standard = thread_registration.new_thread('standard', frozenset(), '.', service_tier=ServiceTier.STANDARD)
    assert FieldCodec.decode(Thread, FieldCodec.encode(standard)).service_tier is ServiceTier.STANDARD
    with pytest.raises(RelationViolationError, match="configuration changed"):
        standard.require_original_model_configuration(restored)


