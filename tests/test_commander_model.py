"""Real Core stores back commander models; the page never executes an order check."""
from dataclasses import replace

import pytest

from agent_comms.goals import CheckedCommit
from agent_comms.ui_model.commander import (
    CommanderModel,
    ObserveCommander,
    OpenCardDetail,
    ReadCommanderIdentity,
)
from agent_comms.ui_model.observation import ReadAnswered, ReadRefused
from test_commander_problem_orders import create


def test_board_uses_order_state_and_requests_details_only_when_opened(commander):
    comms, caller = commander
    lead = comms.registry.require(caller.name).incarnation
    unit = comms.registry.require('worker').incarnation
    create(commander, 'P1', deadline=1000)
    create(commander, 'P2', deadline=2000)
    request = ObserveCommander(str(comms.root), lead)
    model = CommanderModel.read(comms, request)
    assert len(model.units) == 1
    card, = model.units
    assert card.unit == unit
    assert 'P1' in card.line and 'P2' in card.line and 'OVERDUE' in card.line
    assert 'printf' not in card.line and 'Repair the existing owner' not in card.detail.text
    assert len(model.alarms) == 2
    opened = CommanderModel.read(comms, replace(request, expanded=frozenset((unit,))))
    assert isinstance(opened.units[0].detail, OpenCardDetail)
    assert 'Repair the existing owner' in opened.units[0].detail.text
    assert 'Latest work:' in opened.units[0].detail.text
    assert 'Recent turns' in opened.units[0].detail.text


def test_status_source_never_requests_acceptance_measurement(commander, monkeypatch):
    comms, caller = commander
    create(commander, 'P1')
    def forbidden(*args, **kwargs):
        pytest.fail('A read-only commander page executed an acceptance measurement')
    monkeypatch.setattr(CheckedCommit, 'run', forbidden)
    # Progress now consumes retained check receipts, never executes a measurement.
    model = CommanderModel.read(comms, ObserveCommander(
        str(comms.root), comms.registry.require(caller.name).incarnation))
    assert model.units[0].orders[0].progress.state == 'no commit since the order'


def test_commander_identity_is_resolved_or_refused_in_the_read_process(commander):
    comms, caller = commander
    answer = ReadCommanderIdentity(str(comms.root), caller.name).answer(comms)
    assert isinstance(answer, ReadAnswered)
    assert answer.value == ObserveCommander(
        str(comms.root), comms.registry.require(caller.name).incarnation)
    refusal = ReadCommanderIdentity(str(comms.root), 'absent-commander').answer(comms)
    assert isinstance(refusal, ReadRefused)
    assert 'absent-commander' in str(refusal.error)


def test_commander_read_rejects_root_and_incarnation_changes(commander):
    comms, caller = commander
    lead = comms.registry.require(caller.name).incarnation
    request = ObserveCommander(str(comms.root / 'elsewhere'), lead)
    with pytest.raises(ValueError, match='different Comms root'):
        CommanderModel.read(comms, request)
    with pytest.raises(ValueError, match='incarnation changed'):
        CommanderModel.read(comms, replace(request, root=str(comms.root),
                                          commander=replace(lead, created_at=lead.created_at + 1)))
