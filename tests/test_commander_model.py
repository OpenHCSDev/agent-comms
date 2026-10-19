"""Real Core stores back commander models; the page never executes an order check."""
from dataclasses import replace

import pytest

from agent_comms.recorded_time import RecordedEventTime
from agent_comms.todos import CommitMomentum, ForecastCommitMomentum, UnknownCommitMomentum, ProgressPoint, ProgressSource, PullRequests, StatusProgressSource
from agent_comms.ui_model.commander import (
    CommanderModel,
    ObserveCommander,
    OpenCardDetail,
    ReadCommanderIdentity,
    CommanderOrder,
    CardDetail,
    UnitCard,
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
    todo = create(commander, 'P1')
    def forbidden(self):
        pytest.fail('A read-only commander page executed an acceptance measurement')
    monkeypatch.setattr(ProgressSource, 'readings', forbidden)
    source = StatusProgressSource.of(todo, comms.registry.storage_rule, PullRequests((todo,)))
    assert not source.readings().readings
    model = CommanderModel.read(comms, ObserveCommander(
        str(comms.root), comms.registry.require(caller.name).incarnation))
    assert model.units[0].orders[0].progress.state == 'no commit since the order'


def test_card_derives_commit_cadence_against_deadline_without_storing_momentum(commander):
    comms, caller = commander
    todo = replace(create(commander, 'P1'), deadline=RecordedEventTime(300))
    points = (ProgressPoint('first', 100), ProgressPoint('second', 160))
    order = CommanderOrder(todo, ProgressSource.of(todo, comms.registry.storage_rule, PullRequests((todo,))).progress(),
                           False, points, 170)
    card = UnitCard(comms.registry.require('worker').incarnation, (order,), CardDetail())
    momentum, = card.momentum
    assert 'momentum' not in card.__dataclass_fields__
    assert isinstance(momentum, ForecastCommitMomentum)
    assert momentum.interval == 60
    assert momentum.next_at == 220
    assert momentum.deadline_margin == 80
    assert '1.3m before due' in momentum.line
    assert 'completion unknown' in momentum.line
    late = replace(momentum, todo=replace(todo, deadline=RecordedEventTime(200)), observed_at=250)
    assert '0.3m after due' in late.line
    assert 'forecast missed by 0.5m' in late.line


@pytest.mark.parametrize('points', [(), (ProgressPoint('one', 100),),
    (ProgressPoint('one', 100), ProgressPoint('two', 100)),
    (ProgressPoint('one', 160), ProgressPoint('two', 100))])
def test_unknown_commit_cadence_does_not_invent_forecast(commander, points):
    todo = create(commander, 'P1')
    momentum = CommitMomentum.from_commits(todo, points, 170)
    assert isinstance(momentum, UnknownCommitMomentum)
    assert 'cadence unknown' in momentum.line
    assert 'next commit ~' not in momentum.line


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
