"""Order records, slop catalog entries and count checks read through their declared owners."""
import asyncio
import re
from dataclasses import replace

import pytest

from agent_comms.execution.child_process import BoundedRun
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.planning.goals import CountCheckResult
from agent_comms.planning.orders import (
    OrderDelivery,
    RecordedCommitDelivery,
)
from agent_comms.projects.change_report import FileLine
from agent_comms.projects.slop import DeclarationDerivation, ExistingLine, Slop
from agent_comms.storage.recorded_time import RecordedEventTime, UnrecordedEventTime
from source_revision_fixture import SourceRepository
from test_todos import order as checked_order


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
    ("printf 'no count here'", 'printed no integer'),
    ('echo 2', '1 away from'),
    ('echo found 1', None),
])
def test_a_count_check_tells_no_integer_from_a_wrong_count(tmp_path, printed, refused):
    ran = asyncio.run(BoundedRun.run(('bash', '-c', printed), cwd=str(tmp_path), timeout=30))
    if refused is None:
        CountCheckResult(1).require(ran)
    else:
        with pytest.raises(ValueError, match=re.escape(refused)):
            CountCheckResult(1).require(ran)
