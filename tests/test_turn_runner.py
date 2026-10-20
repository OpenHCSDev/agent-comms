"""Mounted ACP orchestration isolation, shutdown and original exception provenance."""

import asyncio

import pytest
from acp import RequestError
from acp.agent.router import build_agent_router

import agent_comms.agent_events as events
import agent_comms.backend as backend
from agent_comms.acp import CommsAgent
from agent_comms.acp_failure import RequestFailure
from agent_comms.acp_extension import InputFailedUpdate, RequestFailedUpdate, decode_updates
from agent_comms.turn_runner import TurnRunner
from stored_inputs import sent_text, stored_inputs


def failure_facts(updates, family):
    return [
        fact
        for update in updates
        for fact in decode_updates(update.field_meta)
        if isinstance(fact, family)
    ]


def test_acp_has_no_superseded_component_accessors_or_dispatch():
    removed = {
        "_sessions",
        "_proxies",
        "_client",
        "_config_options",
        "_setting_requests",
        "_queued_inputs",
        "_dispositions",
        "_schedule_wake",
        "_drain_inbox",
        "_turn_tasks",
        "_turn_locks",
        "_active_turns",
        "_run_agent_turn",
        "_schedule_goal",
        "_goal_store",
        "_persistent_backends",
        "set_goal",
        "compact_context",
        "_sanitized_compaction_summary",
        "config",
    }
    assert not removed.intersection(vars(CommsAgent))
    assert {name for name, value in vars(CommsAgent).items()
            if isinstance(value, property)} == {"configured_root_id"}
