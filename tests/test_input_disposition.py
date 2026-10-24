"""Direct input state survives a process exit without replaying an attempt."""

import multiprocessing
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agent_comms.input_key import AcpInputKey, BusInputKey
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import InputId, MessageId, TurnId
from agent_comms.input_attempt import (
    BoundUnknownInput,
    ClearedNativeInput,
    ClearedUnsentInput,
    InputAlreadyCleared,
    MissingInput,
    MovedInput,
    NotSentInput,
    ReservedInput,
    StartedInput,
    UnchangedInput,
    UnresolvedInput,
)
from agent_comms.input_disposition import InputDispositions
from agent_comms.input_origin import DeliveredInput, UndeliveredInput, WireInputOrigin
from agent_comms.message_reference import MessageReference
from agent_comms.text_digest import TextDigest
from agent_comms.threads import Thread
from stored_inputs import stored_inputs


ORIGINAL_KEY = AcpInputKey(InputId("1" * 32))
LATER_KEY = AcpInputKey(InputId.mint())
KEY_KEY = AcpInputKey(InputId.mint())


def test_unknown_is_durable_and_native_start_is_a_cas(tmp_path: Path) -> None:
    store = InputDispositions(tmp_path)
    assert store.record(BusInputKey(7), seq=7, owner="kid", claim_scope=3, target="kid", text="ask")
    assert not store.record(
        BusInputKey(7), seq=7, owner="kid", claim_scope=3, target="kid", text="ask")
    assert (
        InputDispositions(tmp_path)
        .unresolved(frozenset({"kid"}))
        .unknown(frozenset({"kid"}))[0]
        .sequence
        == 7
    )

    native_id = InputId("a" * 32)
    assert store.bind(
        BusInputKey(7),
        claim_scope=3,
        turn_id=TurnId('974cad2dd603827b06e9e6affa3de03d'),
        native_id=native_id,
        text="full ask",
    )
    assert not store.started(
        BusInputKey(7),
        turn_id=TurnId('974cad2dd603827b06e9e6affa3de03d'),
        native_id=InputId("b" * 32),
        text="full ask",
    )
    assert not store.started(
        BusInputKey(7),
        turn_id=TurnId('974cad2dd603827b06e9e6affa3de03d'),
        native_id=native_id,
        text="wrong",
    )
    assert store.started(
        BusInputKey(7),
        turn_id=TurnId('974cad2dd603827b06e9e6affa3de03d'),
        native_id=native_id,
        text="full ask",
    )
    assert not store.started(
        BusInputKey(7),
        turn_id=TurnId('974cad2dd603827b06e9e6affa3de03d'),
        native_id=native_id,
        text="full ask",
    )
    assert (
        stored_inputs(tmp_path).unknown(frozenset({"kid"}))
        == ()
    )


def test_unresolved_projection_follows_rename_without_private_receipts(tmp_path: Path) -> None:
    import os

    from agent_comms.child_process import ProcessIdentity
    from agent_comms.comms import wire

    comms = wire(tmp_path)
    comms.registry.declare(
        Thread(name='kid', tags=frozenset(), worktree=str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()))
    )
    store = InputDispositions(comms.root)
    store.record(
        BusInputKey(7), seq=7, owner="kid", claim_scope=1, target="#review", text="exact source")
    store.record(
        BusInputKey(8), seq=8, owner="peer", claim_scope=1, target="#review", text="other owner")
    store.bind(
        BusInputKey(7),
        claim_scope=1,
        turn_id=TurnId('9b473845f684444f80a26586ae298d2b'),
        native_id=InputId("a" * 32),
        text="private wrapper",
    )
    comms.threads.rename_managed_thread("kid", "new-kid", owner=ProcessIdentity.capture(os.getpid()))
    expected = (UnresolvedInput(7, "#review", "exact source", BusInputKey(7), "unknown"),)
    assert comms.goals.unresolved_inputs("new-kid") == expected
    assert comms.goals.unresolved_inputs("kid") == expected
    assert store.started(
        BusInputKey(7),
        turn_id=TurnId('9b473845f684444f80a26586ae298d2b'),
        native_id=InputId("a" * 32),
        text="private wrapper",
    )
    assert comms.goals.unresolved_inputs("new-kid") == ()


def _cold_snapshots(root, gate, results):
    store = InputDispositions(Path(root))
    errors = []
    for _ in range(16):
        gate.wait(timeout=10)
        try:
            assert store.lookup(ORIGINAL_KEY).source_text == "original"
        except Exception as error:
            errors.append(f"{type(error).__name__}: {error}")
        gate.wait(timeout=10)
    results.put(errors)


def test_cold_wal_snapshots_do_not_compete_for_initialization(tmp_path):
    store = InputDispositions(tmp_path)
    store.record(ORIGINAL_KEY, seq=None, owner="kid", claim_scope=1, target="kid", text="original")
    context = multiprocessing.get_context("spawn")
    gate, results = context.Barrier(2), context.Queue()
    workers = [context.Process(target=_cold_snapshots, args=(str(tmp_path), gate, results))
               for _ in range(2)]
    try:
        for worker in workers:
            worker.start()
        assert [results.get(timeout=20) for _ in workers] == [[], []]
        for worker in workers:
            worker.join(timeout=5)
            assert worker.exitcode == 0
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=5)
        results.close()
        results.join_thread()


def test_admitted_snapshot_retains_cut_without_blocking_writer(tmp_path):
    store = InputDispositions(tmp_path)
    store.record(ORIGINAL_KEY, seq=None, owner="kid", claim_scope=1, target="kid", text="original")
    with ThreadPoolExecutor(max_workers=1) as writer:
        with store.snapshot() as connection:
            appended = writer.submit(
                store.record, LATER_KEY, seq=None, owner="kid", claim_scope=1,
                target="kid", text="later",
            )
            assert appended.result(timeout=2)
            assert set(store._document(connection, "1", ()).rows) == {ORIGINAL_KEY}
    assert set(store.unresolved(frozenset({"kid"})).rows) == {ORIGINAL_KEY, LATER_KEY}


ROOT, NATIVE, SENT = "c" * 32, "a" * 32, "sent text"
REFERENCE = MessageReference(7, MessageId.mint("original-id"))
RESERVED = ReservedInput(BusInputKey(7), 7, "owner", 1, "owner", "ask",
                         origin=WireInputOrigin(ROOT, REFERENCE))
BOUND = RESERVED._transition(
    BoundUnknownInput,
    turn_id=TurnId('974cad2dd603827b06e9e6affa3de03d'),
    native_id=NATIVE,
    sent_digest=TextDigest.of(SENT),
)
TRANSITIONS = {
    "bind": lambda row: row.bind(
        claim_scope=1,
        turn_id=TurnId('974cad2dd603827b06e9e6affa3de03d'),
        native_id=NATIVE,
        text=SENT,
    ),
    "started": lambda row: row.started(
        turn_id=TurnId('974cad2dd603827b06e9e6affa3de03d'), native_id=NATIVE, text=SENT
    ),
    "finish_unbound": lambda row: row.finish_unbound(),
    "clear_unbound": lambda row: row.clear_unbound(),
    "clear_native": lambda row: row.clear_native(NATIVE, SENT),
}
STATES = {
    "missing": MissingInput(),
    "reserved": RESERVED,
    "bound": BOUND,
    "started": BOUND._transition(StartedInput),
    "not_sent": RESERVED._transition(NotSentInput),
    "cleared_unsent": RESERVED._transition(ClearedUnsentInput),
    "cleared_native": BOUND._transition(ClearedNativeInput),
}
# The state each transition moves a state to; every pair not named leaves its state unchanged.
MOVES = {
    ("reserved", "bind"): BoundUnknownInput,
    ("reserved", "finish_unbound"): NotSentInput,
    ("reserved", "clear_unbound"): ClearedUnsentInput,
    ("bound", "started"): StartedInput,
    ("bound", "clear_native"): ClearedNativeInput,
}
REFUSED = {("cleared_unsent", "bind"), ("cleared_native", "bind")}
UNBOUND_DELIVERIES = {"reserved", "not_sent"}


@pytest.mark.parametrize("state", STATES)
@pytest.mark.parametrize("transition", TRANSITIONS)
def test_every_input_state_answers_every_transition_with_a_member(state, transition) -> None:
    row = STATES[state]
    if (state, transition) in REFUSED:
        with pytest.raises(InputAlreadyCleared):
            TRANSITIONS[transition](row)
        return
    result = TRANSITIONS[transition](row)
    if (state, transition) in MOVES:
        assert isinstance(result, MovedInput)
        assert type(result.state) is MOVES[state, transition]
        assert result.successors(KEY_KEY) == {KEY_KEY: result.state}
        assert result.require_moved("refused") is result
    else:
        assert result == UnchangedInput(row)
        assert result.successors(KEY_KEY) == {}
        with pytest.raises(RelationViolationError):
            result.require_moved("refused")


@pytest.mark.parametrize("state", STATES)
def test_every_input_state_answers_unbound_delivery_with_a_member(state) -> None:
    delivery = STATES[state].unbound_delivery(ROOT)
    if state in UNBOUND_DELIVERIES:
        assert delivery == DeliveredInput(REFERENCE)
        assert delivery.references() == (REFERENCE,)
        assert STATES[state].unbound_delivery("d" * 32).references() == ()
    else:
        assert isinstance(delivery, UndeliveredInput)
        assert delivery.references() == ()
