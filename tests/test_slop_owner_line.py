"""A slop citation's owner is either a line on main or a line the order itself creates."""
import asyncio
import json
from dataclasses import replace

import pytest

from agent_comms.build_identity import RecordedBuildIdentity
from agent_comms.change_report import FileLine
from agent_comms.commander.order import OrderFile
from agent_comms.field_codec import FieldCodec, MalformedRecord
from agent_comms.progress_claims import MergedClaim
from agent_comms.slop import CreatedLine, DeclarationDerivation, ExistingLine, OwnerLine, Slop
from agent_comms.thread_execution import CommanderForkExecution
from agent_comms.threads import Thread
from agent_comms.todos import CommitDelivery, TodoError
from agent_comms.work_branches import WorkBranches
from test_order_scopes import assigned
from test_todos import order

CREATED = FileLine('branch.py', 2)


@pytest.mark.parametrize('wire, owner', (
    ('owner.py:2', ExistingLine(FileLine('owner.py', 2))),
    ({'created': 'branch.py:2'}, CreatedLine(CREATED)),
))
def test_the_boundary_picks_the_member_once_and_encodes_it_back(wire, owner):
    assert FieldCodec.decode(OwnerLine, wire) == owner
    assert FieldCodec.encode(owner) == wire


@pytest.mark.parametrize('wire', ({'created': 'branch.py:2', 'kind': 'x'}, 7))
def test_any_other_owner_shape_is_refused(wire):
    with pytest.raises(MalformedRecord):
        FieldCodec.decode(OwnerLine, wire)


def test_each_member_owns_its_send_and_done_lines():
    existing, created = ExistingLine(FileLine('owner.py', 2)), CreatedLine(CREATED)
    assert (existing.cited(), existing.owed()) == ((existing.line,), ())
    assert (created.cited(), created.owed()) == ((), (CREATED,))


def test_the_order_file_derives_what_the_order_creates_from_its_citations(source_repository):
    slop = Slop('IDEN-5', 'the shape of one branch declaration', FileLine('owner.py', 1),
                DeclarationDerivation, CreatedLine(CREATED))
    document = OrderFile(order=order(source_repository), slop=(slop,),
                         defect='The branch declaration is restated outside its owner file',
                         why_stupid='This is stupid because two files declare one shape')
    read = OrderFile.read(json.dumps(FieldCodec.encode(document)))
    assert read.order.created == (CREATED,)
    assert '(this order creates it)' in read.slop[0].render()


def test_done_is_refused_until_the_delivered_tree_declares_the_created_line(
        comms, source_repository):
    _, current = assigned(comms, source_repository.scope(), source_repository)
    todo = replace(current, order=replace(current.order, created=(CREATED,)))

    def verified(commit: str):
        delivery = CommitDelivery(commit=commit, delivered_commit=commit,
                                  proof_build=RecordedBuildIdentity(commits=()))
        claims = {MergedClaim(commit): (WorkBranches.main,)}
        return asyncio.run(delivery.verified(todo, comms.registry, claims))

    with pytest.raises(TodoError, match='The order creates branch.py:2'):
        verified(source_repository.main_commit)
    assert verified(source_repository.branch_commit).commit == source_repository.branch_commit


def test_a_commander_fork_is_never_a_unit_that_could_take_an_order(tmp_path):
    unit = Thread('unit-a', frozenset(), str(tmp_path))
    fork = replace(Thread('shape-next', frozenset(), str(tmp_path)),
                   execution=CommanderForkExecution)
    assert unit.execution.order_candidates(unit) == (unit,)
    assert fork.execution.order_candidates(fork) == ()
