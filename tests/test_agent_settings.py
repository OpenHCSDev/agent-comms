"""Hierarchical lazy agent settings built on ``objectstate`` and ``FieldCodec``."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from objectstate import ScopeKey
from python_introspect.jsonable import RegisteredClassChoices, registered_class_from_key

from agent_comms.cli_verbs.new import NewThreadCliCommand
from agent_comms.execution.child_process import NO_PROCESS, ProcessIdentity
from agent_comms.execution.pi_vocabulary import (
    HighThinkingLevel,
    LowThinkingLevel,
    ServiceTier,
)
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.agent_settings import AgentSettings
from agent_comms.registry.agent_settings_store import AgentSettingsStore
from agent_comms.registry.agent_settings_wire import (
    AgentOnlySettingsScope,
    AgentSettingsScope,
    ChannelSettingsScope,
    GlobalSettingsScope,
    ProjectSettingsScope,
    ScopedSettingsProjection,
    ThreadSettingsScope,
)
from agent_comms.registry.native_settings import NativeThreadDefaults
from agent_comms.registry.thread_model import CliInboxThreadModel, DeclaredModel
from agent_comms.registry.thread_settings import ContextEditPolicy
from agent_comms.registry.thread_status import StoppedThreadStatus
from agent_comms.team.pi_rule_gate import PiThreadGate
from agent_comms.team.rule_deal import (
    CompactionDealMode,
    DealMode,
    FullDealMode,
    OffRuleCheckPolicy,
    RequiredRuleCheckPolicy,
    RuleCheckPolicy,
)
from agent_comms.transport.tools.sidebar import (
    CommsAgentSettingsTool,
    CommsSetAgentSettingsTool,
)
from tests.conftest import ui_core_read


def test_scope_keys_match_objectstate_scope_key_contract() -> None:
    cases = (
        (GlobalSettingsScope(), ScopeKey("")),
        (ProjectSettingsScope("agent-comms"), ScopeKey.of("project", "agent-comms")),
        (
            ChannelSettingsScope("agent-comms", "#comms"),
            ScopeKey.of("project", "agent-comms", "channel", "#comms"),
        ),
        (
            ThreadSettingsScope("agent-comms", "comms-a"),
            ScopeKey.of("project", "agent-comms", "thread", "comms-a"),
        ),
        (AgentOnlySettingsScope("solo"), ScopeKey.of("agent", "solo")),
    )
    for scope, expected_key in cases:
        assert scope.scope_id == str(expected_key)
        assert AgentSettingsScope.parse(scope.scope_id) == scope

    for bad in ("project", "project::", "project::a::unknown::b", "unknown::x", "::a"):
        with pytest.raises(ValueError):
            AgentSettingsScope.parse(bad)


def test_four_tier_lazy_inheritance_provenance_cascade_and_reset(tmp_path: Path) -> None:
    settings = AgentSettings.at(tmp_path)
    global_scope = GlobalSettingsScope()
    project_scope = ProjectSettingsScope("agent-comms")
    channel_scope = ChannelSettingsScope("agent-comms", "#comms")
    thread_scope = ThreadSettingsScope("agent-comms", "comms-a")

    # Baseline: everything inherits from GlobalAgentSettings defaults.
    initial = settings.resolve(thread_scope, channel="#comms")
    assert initial.values.thread.model == NativeThreadDefaults.model
    assert initial.values.thread.thinking_level is HighThinkingLevel
    assert initial.values.thread.service_tier is ServiceTier.STANDARD
    assert initial.values.thread.context_edit_policy is ContextEditPolicy.OPTIONAL
    assert initial.values.rule_gate.mode is CompactionDealMode
    assert initial.values.rule_gate.audit is OffRuleCheckPolicy

    # Author overrides at project, channel, and thread scopes.
    settings.update_scope(project_scope, {"thread.thinking_level": "low"})
    settings.update_scope(channel_scope, {"rule_gate.mode": "full", "rule_gate.audit": "required"})
    settings.update_scope(
        thread_scope,
        {
            "thread.context_edit_policy": "required_each_turn",
            "thread.model": "cli-inbox",
        },
        channel="#comms",
    )

    resolved = settings.resolve(thread_scope, channel="#comms")
    assert resolved.values.thread.model == CliInboxThreadModel()
    assert resolved.values.thread.thinking_level is LowThinkingLevel
    assert resolved.values.thread.context_edit_policy is ContextEditPolicy.REQUIRED_EACH_TURN
    assert resolved.values.rule_gate.mode is FullDealMode
    assert resolved.values.rule_gate.audit is RequiredRuleCheckPolicy

    projection = resolved.to_projection()
    assert projection.scope == "project::agent-comms::thread::comms-a"
    assert projection.ancestry == (
        "",
        "project::agent-comms",
        "project::agent-comms::channel::#comms",
        "project::agent-comms::thread::comms-a",
    )
    assert projection.layers == {
        "": {},
        "project::agent-comms": {"thread.thinking_level": "low"},
        "project::agent-comms::channel::#comms": {
            "rule_gate.mode": "full",
            "rule_gate.audit": "required",
        },
        "project::agent-comms::thread::comms-a": {
            "thread.context_edit_policy": "required_each_turn",
            "thread.model": "cli-inbox",
        },
    }
    assert projection.resolved["thread.model"].source_scope == thread_scope.scope_id
    assert projection.resolved["thread.model"].authored is True
    assert projection.resolved["thread.thinking_level"].source_scope == project_scope.scope_id
    assert projection.resolved["thread.thinking_level"].source_type == "ThreadSettings"
    assert projection.resolved["thread.thinking_level"].authored is False
    assert projection.resolved["rule_gate.mode"].source_scope == channel_scope.scope_id
    assert projection.resolved["rule_gate.mode"].source_type == "RuleGateSettings"
    assert projection.resolved["rule_gate.owner_prompts"].source_scope == ""

    # Updating global cascades to un-overridden descendants; resetting thread falls back to global.
    settings.update_scope(global_scope, {"thread.model": "anthropic/claude-opus-4-6"})
    settings.update_scope(thread_scope, {"thread.model": None}, channel="#comms")
    after_reset = settings.resolve(thread_scope, channel="#comms")
    assert after_reset.values.thread.model == DeclaredModel("anthropic/claude-opus-4-6")
    assert after_reset.to_projection().resolved["thread.model"].source_scope == ""
    assert after_reset.to_projection().resolved["thread.model"].authored is False

    # Clearing the last authored override at a scope prunes that scope entry from disk.
    settings.update_scope(project_scope, {"thread.thinking_level": None})
    assert project_scope.scope_id not in AgentSettingsStore.at(tmp_path).read().scopes


def test_unknown_field_and_invalid_value_fail_loud(tmp_path: Path) -> None:
    settings = AgentSettings.at(tmp_path)
    with pytest.raises(ValueError, match="Unknown agent setting path"):
        settings.update_scope(GlobalSettingsScope(), {"thread.not_a_field": "x"})
    with pytest.raises(Exception):
        settings.update_scope(GlobalSettingsScope(), {"rule_gate.owner_prompts": 0})


def test_thread_rename_and_delete_update_owned_scopes(comms, tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    owner = ProcessIdentity.capture(os.getpid())
    worker = comms.threads.claim_thread(
        "worker", tags=frozenset(), worktree=Checkout(project), process=owner
    )
    settings = AgentSettings.at(comms.root)

    settings.update_scope(GlobalSettingsScope(), {"rule_gate.owner_prompts": 8})
    settings.update_scope(ProjectSettingsScope("proj"), {"thread.thinking_level": "low"})
    settings.update_scope(ThreadSettingsScope("proj", "worker"), {"thread.service_tier": "priority"})
    settings.update_scope(AgentOnlySettingsScope("worker"), {"rule_gate.audit": "required"})

    comms.threads.rename_managed_thread(worker.incarnation, "renamed-worker", owner=owner)
    saved = AgentSettingsStore.at(comms.root).read().scopes
    assert "project::proj::thread::worker" not in saved
    assert "agent::worker" not in saved
    assert saved["project::proj::thread::renamed-worker"] == {"thread.service_tier": "priority"}
    assert saved["agent::renamed-worker"] == {"rule_gate.audit": "required"}
    assert saved[""] == {"rule_gate.owner_prompts": 8}
    assert saved["project::proj"] == {"thread.thinking_level": "low"}

    comms.registry.register(
        replace(comms.registry.require("renamed-worker"), process_identity=NO_PROCESS),
        status=StoppedThreadStatus(),
    )
    comms.threads.delete("renamed-worker")
    after_delete = AgentSettingsStore.at(comms.root).read().scopes
    assert "project::proj::thread::renamed-worker" not in after_delete
    assert "agent::renamed-worker" not in after_delete
    assert after_delete[""] == {"rule_gate.owner_prompts": 8}
    assert after_delete["project::proj"] == {"thread.thinking_level": "low"}


def test_served_mcp_tools_and_wire_codec_round_trip(comms) -> None:
    updated = ui_core_read(
        comms,
        CommsSetAgentSettingsTool(
            scope="project::agent-comms",
            values={"thread.thinking_level": "low", "rule_gate.audit": "required"},
        ),
    )
    assert isinstance(updated, ScopedSettingsProjection)
    assert updated.layers["project::agent-comms"] == {
        "thread.thinking_level": "low",
        "rule_gate.audit": "required",
    }

    read_back = ui_core_read(
        comms,
        CommsAgentSettingsTool(
            scope="project::agent-comms::thread::comms-a",
            channel="#comms",
        ),
    )
    decoded = FieldCodec.decode(ScopedSettingsProjection, FieldCodec.encode(read_back))
    assert decoded == read_back
    assert decoded.resolved["thread.thinking_level"].value == "low"
    assert decoded.resolved["thread.thinking_level"].source_scope == "project::agent-comms"
    assert decoded.resolved["rule_gate.audit"].value == "required"
    assert decoded.resolved["rule_gate.audit"].authored is False


def test_agent_settings_wire_and_store_stay_light_without_objectstate_or_rule_deal() -> None:
    source = Path(__file__).resolve().parents[1] / "src"
    program = f"""
import json, sys
sys.path.insert(0, {str(source)!r})
import agent_comms.registry.agent_settings_wire
import agent_comms.registry.agent_settings_store
import agent_comms.transport.tools.sidebar
print(json.dumps({{
    "objectstate": "objectstate" in sys.modules,
    "rule_deal": "agent_comms.team.rule_deal" in sys.modules,
    "agent_settings": "agent_comms.registry.agent_settings" in sys.modules,
}}))
"""
    completed = subprocess.run(
        [sys.executable, "-I", "-c", program],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    loaded = json.loads(completed.stdout)
    assert loaded == {"objectstate": False, "rule_deal": False, "agent_settings": False}


def test_new_thread_bindings_and_undealt_pi_gate_inherit_scoped_settings(comms, tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    settings = AgentSettings.at(comms.root)
    settings.update_scope(GlobalSettingsScope(), {"thread.model": "anthropic/claude-opus-4-6"})
    settings.update_scope(
        ProjectSettingsScope("proj"),
        {
            "thread.thinking_level": "low",
            "thread.service_tier": "priority",
            "rule_gate.mode": "full",
            "rule_gate.audit": "required",
        },
    )

    binding, = NewThreadCliCommand.project_bindings(comms, Checkout(project))
    assert binding.model == DeclaredModel("anthropic/claude-opus-4-6")
    assert binding.thinking_level is LowThinkingLevel
    assert binding.service_tier is ServiceTier.PRIORITY

    worker = comms.threads.claim_thread("worker", tags=frozenset(), worktree=Checkout(project))
    gate = PiThreadGate.of(comms, worker)
    record = gate.stored()
    assert record.mode is FullDealMode
    assert record.audit is RequiredRuleCheckPolicy


def test_declared_family_exposes_registry_key_for_registered_class_choices() -> None:
    mode_choices = dict(RegisteredClassChoices(DealMode).labelled_choices())
    assert mode_choices["compaction"] is CompactionDealMode
    assert mode_choices["full"] is FullDealMode
    assert registered_class_from_key(DealMode, "compaction") is CompactionDealMode

    policy_choices = dict(RegisteredClassChoices(RuleCheckPolicy).labelled_choices())
    assert policy_choices["off"] is OffRuleCheckPolicy
    assert policy_choices["required"] is RequiredRuleCheckPolicy
