"""One unit Goal reads its independent canonical Todo orders through real tools."""
import asyncio
from dataclasses import replace

import pytest

from test_todos import order as checked_order
from source_revision_fixture import SourceRepository


from agent_comms.change_report import FileLine
from agent_comms.child_process import BoundedRun
from agent_comms.field_codec import FieldCodec
from agent_comms.goals import CountCheckResult
from agent_comms.recorded_time import RecordedEventTime, UnrecordedEventTime
from agent_comms.todos import (
    OrderDelivery,
    RecordedCommitDelivery,
)
from agent_comms.slop import DeclarationDerivation, ExistingLine, Slop


def order(source: SourceRepository, identity, command='printf 1', expected=1):
    declaration = checked_order(source)
    return replace(declaration, id=identity,
        check=replace(declaration.check, command=command, result=CountCheckResult(expected)))


def test_a_done_record_from_before_the_split_reads_its_one_commit_as_both():
    old = {'kind': 'commit', 'commit': 'd012d23b4', 'at': FieldCodec.encode(RecordedEventTime(1000))}
    recorded = FieldCodec.decode(OrderDelivery, old)
    assert type(recorded) is RecordedCommitDelivery and recorded.delivered == recorded.commit == 'd012d23b4'
    assert FieldCodec.encode(recorded) == old


def test_delivery_time_has_its_own_named_requirement():
    with pytest.raises(ValueError):
        UnrecordedEventTime().require_delivery()


def test_slop_uses_every_pinned_catalog_entry_and_refuses_unknown_ids():
    location = FileLine(__file__, 1)
    for pattern in Slop.patterns():
        slop = Slop(pattern, 'the count of open orders', location, DeclarationDerivation,
                    ExistingLine(location))
        assert FieldCodec.decode(Slop, FieldCodec.encode(slop)) == slop
        assert pattern in slop.render()
    with pytest.raises(ValueError):
        Slop('MADEUP-999', 'the count of open orders', location, DeclarationDerivation,
             ExistingLine(location))


@pytest.mark.parametrize('printed, refused', [
    ("printf 'no count here'", 'The order check cannot prove CountCheckResult(expected=1): it printed no integer.'),
    ('echo 2', 'The order check is 1 away from CountCheckResult(expected=1).'),
    ('echo found 1', None),
])
def test_a_count_check_refuses_saying_whether_it_printed_no_integer_or_the_wrong_count(tmp_path, printed, refused):
    ran = asyncio.run(BoundedRun.run(('bash', '-c', printed), cwd=str(tmp_path), timeout=30))
    if refused is None:
        CountCheckResult(1).require(ran)
    else:
        with pytest.raises(ValueError):
            CountCheckResult(1).require(ran)



