"""Scoped claim-envelope durability on an initialized private root.

These tests exercise the publisher boundary, not native input authorization.
Ordinary conversation sends no longer acquire or manually release files.
"""
from __future__ import annotations



from bus_history import read_history
import json
import os
import secrets
import signal
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import replace
from functools import partial
from pathlib import Path
from threading import Barrier

import pytest

from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms, wire
from agent_comms.coordination_session import NativeBoundary
from agent_comms.envelope_claim_transitions import ClaimConflict, ExistingFileClaim, NativeClaim
from agent_comms.errors import RelationViolationError
from agent_comms.exporting import EverythingScope, FullLimit, JsonlFormat
from agent_comms.field_codec import TurnId
from agent_comms.messages import Message
from agent_comms.message_reference import MessageReference
from agent_comms.thread_status import RunningThreadStatus
from agent_comms.threads import Thread


def _participants(tmp_path: Path) -> tuple[Comms, Path]:
    worktree = tmp_path / "project"
    worktree.mkdir()
    (worktree / "a.py").write_text("one")
    (worktree / "b.py").write_text("two")
    comms = wire(tmp_path / "wire")
    for name in ("alice", "bob", "observer"):
        owner = Thread(name, frozenset({'team'}), str(worktree))
        comms.registry.declare(
            replace(owner, process_identity=ProcessIdentity.capture(os.getpid())),
            RunningThreadStatus(),
        )
    return comms, worktree


def acquire_message(comms: Comms, name: str, path: Path, turn_id: TurnId) -> Message:
    """Publish one scoped tool acquisition through the production claim writer."""
    with NativeBoundary(comms.bus) as boundary:
        owner = boundary.registry.require(name)
        scope = NativeClaim(
            owner_claimed_generation=boundary.registry.claim_generation(name),
            operation_id=secrets.token_hex(16), turn_id=turn_id, input_id=f"input-{name}",
        )
        claim = comms.bus.publisher._publish_acquisition_unlocked(
            boundary.registry, owner,
            ExistingFileClaim(path).normalized(Path(owner.worktree)), scope,
            activity=boundary.activity,
        )
    message, = comms.bus.bus.messages_for_references((MessageReference(claim.seq, claim.message_id),))
    return message


def release_messages(comms: Comms, messages: tuple[Message, ...]) -> None:
    """Retire the actual acquisition records, not caller-authored release paths."""
    claims = tuple(message.claim_transition.claimed(resource)
                   for message in messages for resource in message.claim_transition.claims)
    with NativeBoundary(comms.bus) as boundary:
        comms.bus.publisher.release_resources(boundary, claims, "Finished test tool operation")


def test_scoped_acquisition_is_one_original_and_conflict_appends_nothing(tmp_path):
    turn_id = TurnId.mint()
    comms, worktree = _participants(tmp_path)
    sent = acquire_message(comms, "alice", Path("a.py"), turn_id=turn_id)
    transition = sent.claim_transition
    assert transition.claims == (str(worktree / "a.py"),)
    assert (transition.message_id, transition.seq) == (sent.message_id, sent.seq)
    assert isinstance(transition.resource_scope, NativeClaim)
    assert transition.resource_scope.input_id == "input-alice"
    assert comms.bus.bus.revision() == sent.seq
    assert comms.bus.bus.claim(sent.seq).resource_scope.input_id == "input-alice"
    assert read_history(comms) == [sent]
    assert comms.bus.bus.claim_projection()[str(worktree / "a.py")].owner == "alice"
    with pytest.raises(ClaimConflict) as loss:
        acquire_message(comms, "bob", Path("a.py"), turn_id=turn_id)
    assert loss.value.existing.owner == "alice"
    assert read_history(wire(comms.root)) == [sent]


def test_absolute_claim_outside_worktree_uses_one_global_key(tmp_path):
    turn_id = TurnId.mint()
    comms, worktree = _participants(tmp_path)
    outside = tmp_path / "shared" / "module.py"
    outside.parent.mkdir()
    outside.write_text("shared")
    claimed = acquire_message(comms, "alice", outside, turn_id=turn_id)
    assert claimed.claim_transition.claims == (str(outside),)
    projection = wire(comms.root).bus.bus.claim_projection()
    assert list(projection) == [str(outside)]
    assert projection[str(outside)].owner == "alice"
    with pytest.raises(ClaimConflict):
        acquire_message(comms, "bob", outside, turn_id=turn_id)
    release_messages(comms, (claimed,))
    assert not comms.bus.bus.claim_projection()
    assert worktree != outside.parent


def test_same_file_double_slash_alias_conflicts_without_second_message(tmp_path):
    turn_id = TurnId.mint()
    comms, worktree = _participants(tmp_path)
    canonical = str(worktree / "a.py")
    alias = str(worktree) + "//a.py"
    assert os.path.samefile(canonical, alias) and canonical != alias
    first = acquire_message(comms, "alice", Path(canonical), turn_id=turn_id)
    with pytest.raises(ClaimConflict) as conflict:
        acquire_message(comms, "bob", Path(alias), turn_id=turn_id)
    assert conflict.value.existing.owner == "alice"
    assert read_history(comms) == [first]
    assert dict(comms.bus.bus.claim_projection()) == {canonical: conflict.value.existing}


def test_same_tick_new_owner_cannot_share_live_claim_identity(tmp_path):
    turn_id = TurnId.mint()
    comms, worktree = _participants(tmp_path)
    claimed = acquire_message(comms, "alice", Path("a.py"), turn_id=turn_id)
    alice = comms.registry.require("alice")
    with pytest.raises(RelationViolationError):
        comms.registry.declare(Thread(
            'same-tick-peer', frozenset({'team'}), str(worktree), created_at=alice.created_at))
    assert "same-tick-peer" not in comms.registry
    assert read_history(comms) == [claimed]
    assert comms.bus.bus.claim_projection()[str(worktree / "a.py")].owner == "alice"
    release_messages(comms, (claimed,))
    assert str(worktree / "a.py") not in comms.bus.bus.claim_projection()


def test_saved_colliding_creation_id_blocks_claim_write_before_append(tmp_path):
    turn_id = TurnId.mint()
    comms, _ = _participants(tmp_path)
    claimed = acquire_message(comms, "alice", Path("a.py"), turn_id=turn_id)
    with comms.registry.store.editing() as edit:
        threads = edit.document.threads
        threads["bob"] = replace(threads["bob"], created_at=threads["alice"].created_at)
        edit.commit()
    reopened = wire(comms.root)
    with pytest.raises(RelationViolationError):
        acquire_message(reopened, "bob", Path("a.py"), turn_id=turn_id)
    assert reopened.bus.bus.since(0, 2) == (claimed,)
    with pytest.raises(RelationViolationError):
        dict(reopened.registry.snapshot().by_lookup)


def test_owner_can_release_acquisition_after_file_deletion(tmp_path):
    turn_id = TurnId.mint()
    comms, worktree = _participants(tmp_path)
    claimed = acquire_message(comms, "alice", Path("a.py"), turn_id=turn_id)
    (worktree / "a.py").unlink()
    release_messages(comms, (claimed,))
    assert comms.bus.bus.claim_projection().get(str(worktree / "a.py")) is None
    assert len(read_history(comms)) == 2


def test_concurrent_acquisition_exactly_one_winner(tmp_path):
    turn_id = TurnId.mint()
    comms, _ = _participants(tmp_path)
    simultaneous = Barrier(2)

    def attempt(name):
        simultaneous.wait(timeout=3)
        try:
            acquire_message(wire(comms.root), name, Path("a.py"), turn_id=turn_id)
        except ClaimConflict:
            return "lost"
        return "won"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(attempt, ("alice", "bob")))
    assert sorted(results) == ["lost", "won"]
    assert len(read_history(comms)) == 1


def test_large_release_reopens_and_exports_without_a_second_parser_limit(tmp_path):
    turn_id = TurnId.mint()
    comms, worktree = _participants(tmp_path)
    directory = worktree
    for number in range(10):
        directory = directory / ("d" * 180 + str(number))
        directory.mkdir()
    acquired = []
    for number in range(10):
        resource = directory / f"{number}.py"
        resource.write_text("ordinary file")
        acquired.append(acquire_message(comms, "alice", resource, turn_id=turn_id))
    assert len(comms.bus.bus.claim_projection()) == len(acquired)
    release_messages(comms, tuple(acquired))
    history = read_history(comms)
    committed = history[-1]
    assert len(json.dumps(committed.to_wire()["claim_transition"])) > 16_384
    assert not wire(comms.root).bus.bus.claim_projection()
    assert read_history(wire(comms.root)) == history
    assert comms.bus.bus.messages_for_references((committed.reference,)) == (committed,)
    destination = tmp_path / "safe-export.jsonl"
    acknowledgement = comms.views.export_wire(
        destination, format=JsonlFormat(), scope=EverythingScope(), limit=FullLimit())
    exported = [json.loads(line) for line in destination.read_text().splitlines()]
    assert acknowledgement.exported_messages == len(history)
    assert len(exported) == len(history) + 1 and exported[-1]["record"] == "message"
    assert exported[-1]["message"] == Message.from_message(committed).to_wire()


KILL_BEFORE_COMMIT = """
real_insert = Bus._insert
def kill_inside_transaction(self, db, build):
    real_insert(self, db, build)
    os.kill(os.getpid(), signal.SIGKILL)
Bus._insert = kill_inside_transaction
"""
KILL_AFTER_COMMIT = """
def kill_at_notification(cls, path):
    os.kill(os.getpid(), signal.SIGKILL)
Bus.publish_change = classmethod(kill_at_notification)
"""


@pytest.mark.parametrize(("kill", "committed", "competitor"), [
    (KILL_BEFORE_COMMIT, 0, nullcontext),
    (KILL_AFTER_COMMIT, 1, partial(pytest.raises, ClaimConflict)),
])
def test_sigkill_across_claim_acquisition_recovers_one_row_or_none(
    tmp_path, kill, committed, competitor,
):
    """A publisher killed inside or just after its transaction leaves the whole row or none."""
    turn_id = TurnId.mint()
    comms, _ = _participants(tmp_path)
    code = """
import os, signal, sys
from pathlib import Path
sys.path.insert(0, sys.argv[2])
sys.path.insert(0, str(Path(sys.argv[2]).parent / 'tests'))
from agent_comms.bus import Bus
from agent_comms.comms import wire
from agent_comms.field_codec import TurnId
from test_envelope_bus_integration import acquire_message
""" + kill + """
turn_id = TurnId.mint()
acquire_message(wire(Path(sys.argv[1])), 'alice', Path('a.py'), turn_id=turn_id)
"""
    source = str(Path(__file__).resolve().parents[1] / "src")
    env = os.environ.copy()
    for key in ("PI_PARENT_ID", "PI_AGENT_TAGS", "AGENT_COMMS_THREAD", "AGENT_COMMS_TAGS"):
        env.pop(key, None)
    child = subprocess.run(
        [sys.executable, "-c", code, str(comms.root), source],
        env=env, capture_output=True, check=False, timeout=8,
    )
    assert child.returncode == -signal.SIGKILL, child.stderr.decode(errors="replace")
    rows = read_history(comms)
    assert len(rows) == committed
    assert all(row.claim_transition is not None for row in rows)
    with competitor():
        acquire_message(comms, "bob", Path("a.py"), turn_id=turn_id)
    assert len(read_history(comms)) == 1
