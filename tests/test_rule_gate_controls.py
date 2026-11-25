"""Recovery stays reachable; audit checks do not decide when rules return."""

from contextlib import nullcontext
from dataclasses import replace

import pytest

from agent_comms import agent_events as events
from agent_comms.cli_verbs.cli_command import PreparedTargetAction, TargetAction
from agent_comms.cli_verbs.configure_rule_gate import ThreadRuleGateCliCommand
from agent_comms.execution.context_reading import ContextUsage
from agent_comms.nominal.field_codec import FieldCodec, ToolCallId
from agent_comms.projects.rule_probes import (
    HuntProbe, NoEditsHuntProbe, Probe, ProbeContext,
)
from agent_comms.team.pi_rule_gate import PiRedeal, RulesUnread
from agent_comms.team.rule_deal import (
    EmptyDealDelivery,
    FullDealMode,
    OffDealMode,
    OffRuleCheckPolicy,
    RequiredRuleCheckPolicy,
    RuleGateSettings,
)
from agent_comms.transport.native_tools import NativeToolDeclaration, ReadTool
from agent_comms.transport.tool_caller import ThreadCaller
from agent_comms.transport.served_tools.orders import ServedCommsEmergencyReleaseTool
from conftest import answer_deal


def test_a_read_only_task_can_answer_without_inventing_a_source_finding(tmp_path):
    answer = FieldCodec.decode(Probe, {
        "probe": "no_edits_hunt", "reason": "Find and clone personal-model; inspect its brief only.",
    })
    assert isinstance(answer, NoEditsHuntProbe)
    assert not answer.fault(ProbeContext((), tmp_path))
    assert HuntProbe not in Probe.missing((answer,), ())
    assert NoEditsHuntProbe not in Probe.owed(())


@pytest.mark.asyncio
async def test_rule_settings_prepare_both_choices_and_apply_through_the_command(team_bus):
    gate = PiRedeal.of(team_bus, team_bus.registry.require("unit-x"))
    gate.start()
    command = ThreadRuleGateCliCommand(name="unit-x")
    action = TargetAction((command,), ("unit-x",), ThreadRuleGateCliCommand)
    prepared = await action.prepared(team_bus.configuration_catalogs)
    received = FieldCodec.decode(PreparedTargetAction, FieldCodec.encode(prepared))
    choices = {field.name: {choice.name: choice.value for choice in field.offered()}
               for field in received.fields}
    assert set(choices["audit"]) == {"off", "required"}
    assert set(choices["confirmation"]) == {"off", "required"}
    assert not received.confirmation
    edited = received.edited({"audit": choices["audit"]["off"],
                              "confirmation": choices["confirmation"]["off"]}).with_confirmation(False)
    edited.bound[0].apply(team_bus)
    assert gate.current().audit is OffRuleCheckPolicy
    assert gate.current().audit_confirmation is OffRuleCheckPolicy
    assert FieldCodec.encode(gate.current())["confirmation"] == "off"


def test_emergency_release_runs_while_locked_and_preserves_evidence(team_bus, monkeypatch):
    monkeypatch.setattr(PiRedeal, "delivery", lambda self: EmptyDealDelivery())
    gate = PiRedeal.of(team_bus, team_bus.registry.require("unit-x"))
    gate.start()
    audit = gate.audit.read_bytes()
    sources = gate.current().dealt
    bash = NativeToolDeclaration.decode("bash", "bash", {"command": "true"})
    with pytest.raises(RulesUnread):
        bash.judge_under(gate)

    release = ServedCommsEmergencyReleaseTool()
    release.create_call(ToolCallId("emergency"), {}).judge_under(gate)
    result = release.apply(team_bus, ThreadCaller("unit-x"))

    assert result.notice
    assert gate.current().mode is OffDealMode
    assert gate.current().dealt == sources
    assert gate.audit.read_bytes() == audit
    bash.judge_under(gate)


@pytest.mark.parametrize("reads, after_compaction", (
    (OffRuleCheckPolicy, nullcontext()),
    (RequiredRuleCheckPolicy, pytest.raises(RulesUnread)),
))
def test_audit_off_keeps_read_policy_and_compaction_refresh(
    team_bus, monkeypatch, reads, after_compaction
):
    monkeypatch.setattr(PiRedeal, "delivery", lambda self: EmptyDealDelivery())
    gate = PiRedeal.of(team_bus, team_bus.registry.require("unit-x"))
    gate.start()
    gate.configure(RuleGateSettings(mode=FullDealMode, required_reads=reads, audit=OffRuleCheckPolicy))
    sources = gate.current().dealt
    for source in sources:
        source.read_text()
        gate.record_read(source, ReadTool.default_span())
    gate.audit.write_text("not an audit")
    gate.judge_tool("bash")
    gate.audit_written()
    assert not gate.meant.read().open

    events.CompactionEnd().deal_rules(gate)

    assert gate.current().mode is FullDealMode
    assert gate.current().audit is OffRuleCheckPolicy
    assert gate.current().required_reads is reads
    assert gate.current().dealt == sources
    assert not gate.current().read_by(gate)
    with after_compaction:
        gate.judge_tool("bash")
    for source in sources:
        source.read_text()
        gate.record_read(source, ReadTool.default_span())
    gate.judge_tool("bash")
    gate.observe_context(ContextUsage(70, 100))
    assert gate.current().audit is OffRuleCheckPolicy
    assert gate.current().trigger.used == 70


def test_confirmation_can_be_disabled_without_disabling_audit(team_bus):
    gate = PiRedeal.of(team_bus, team_bus.registry.require("unit-x"))
    gate.start()
    gate.configure(RuleGateSettings(audit_confirmation=OffRuleCheckPolicy))
    answer_deal(gate)
    gate.audit_written()
    assert not gate.meant.read().open
    gate.judge_tool("bash")
    assert FieldCodec.encode(RuleGateSettings.capture(gate.current()))["confirmation"] == "off"
    gate.configure(replace(RuleGateSettings.capture(gate.current()), audit=OffRuleCheckPolicy))
    assert gate.current().audit_confirmation is OffRuleCheckPolicy
