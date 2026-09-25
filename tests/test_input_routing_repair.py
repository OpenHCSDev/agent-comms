"""Historical attribution requires owner-bound native IDs plus committed envelopes."""

import json
import sqlite3

from agent_comms import Thread, wire
from agent_comms.cli import main
from agent_comms.declarations import ScheduledTurn
from agent_comms.input_disposition import InputDispositions


def fixture(tmp_path):
    comms = wire(tmp_path / "wire")
    for name in ("peer", "worker"):
        comms.register(Thread(name, frozenset(), str(tmp_path)))
    message = comms.send_message("peer", "worker", "Review the implementation")
    source = ScheduledTurn.incoming(message).prompt
    sent = "Coordination context: owner instructions\n\n" + source
    dispositions = InputDispositions(comms.root)
    key = f"bus:{message.seq}"
    dispositions.record(
        key, seq=message.seq, owner="worker", admission=1, target="worker", text=source
    )
    assert dispositions.bind(key, admission=1, turn_id="turn", native_id="a" * 32, text=sent)
    path = tmp_path / "native.jsonl"
    path.write_text(
        json.dumps(
            {
                "type": "message",
                "id": "saved-input",
                "message": {
                    "role": "user",
                    "inputId": "a" * 32,
                    "content": [{"type": "text", "text": sent}],
                },
            }
        )
        + "\n"
    )
    comms.attach_session("worker", str(path))
    return comms, message, dispositions, source, sent


def test_preview_is_read_only_and_apply_is_idempotent_without_ack(tmp_path):
    comms, message, dispositions, _, _ = fixture(tmp_path)
    before = dispositions.path.read_bytes()
    pending = comms.pending_count("worker")
    assert not comms.transcript_routes.database_path.exists()
    preview = comms.repair_input_routing()
    assert preview == {
        "dry_run": True,
        "eligible": 1,
        "already_bound": 0,
        "repaired": 0,
        "skipped": 0,
        "conflicts": 0,
    }
    assert not comms.transcript_routes.database_path.exists()
    applied = comms.repair_input_routing(dry_run=False)
    assert applied["repaired"] == 1
    assert comms.repair_input_routing(dry_run=False)["already_bound"] == 1
    event = wire(comms.root).thread_transcript_page("worker").events[0]
    assert event.text == message.body and event.routing.requests == (message,)
    assert dispositions.path.read_bytes() == before
    assert dispositions.status(f"bus:{message.seq}") == "unknown"
    assert comms.pending_count("worker") == pending


def test_old_display_schema_survives_and_dry_run_does_not_upgrade_it(tmp_path):
    comms, message, _, source, _ = fixture(tmp_path)
    comms.record_input_display("a" * 32, source)
    with sqlite3.connect(comms.transcript_routes.database_path) as connection:
        connection.execute("DROP TABLE input_routing")
    comms = wire(comms.root)
    assert comms.repair_input_routing()["eligible"] == 1
    with sqlite3.connect(comms.transcript_routes.database_path) as connection:
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name='input_routing'"
            ).fetchone()
            is None
        )
    assert comms.repair_input_routing(dry_run=False)["repaired"] == 1
    # A running old writer's exact two-value insert remains valid.
    with sqlite3.connect(comms.transcript_routes.database_path) as connection:
        connection.execute("INSERT INTO input_display VALUES (?, ?)", ("b" * 32, "old writer"))
    event = comms.thread_transcript_page("worker").events[0]
    assert event.routing.requests[0].message_id == message.message_id


def test_ambiguous_native_id_or_mismatched_receipt_is_not_repaired(tmp_path):
    comms, message, dispositions, source, sent = fixture(tmp_path)
    second = comms.send_message("peer", "worker", "Second message")
    key = f"bus:{second.seq}"
    dispositions.record(
        key,
        seq=second.seq,
        owner="worker",
        admission=1,
        target="worker",
        text=ScheduledTurn.incoming(second).prompt,
    )
    dispositions.bind(key, admission=1, turn_id="turn", native_id="a" * 32, text=sent)
    result = comms.repair_input_routing(dry_run=False)
    assert result["eligible"] == result["repaired"] == 0 and result["conflicts"] == 2
    assert not comms.transcript_routes.database_path.exists()


def test_existing_different_binding_is_not_overwritten(tmp_path):
    comms, _, _, _, sent = fixture(tmp_path)
    comms.record_input_display("a" * 32, "Explicit human input", sent_text=sent)
    before = comms.transcript_routes.input_bindings()
    assert comms.repair_input_routing()["conflicts"] == 1
    assert comms.repair_input_routing(dry_run=False)["conflicts"] == 1
    assert comms.transcript_routes.input_bindings() == before


def test_unbound_or_quoted_input_does_not_supply_provenance(tmp_path):
    comms, _, dispositions, source, _ = fixture(tmp_path)
    another = comms.send_message("peer", "worker", "Different body")
    dispositions.record(
        f"bus:{another.seq}",
        seq=another.seq,
        owner="worker",
        admission=1,
        target="worker",
        text=source,
    )
    dispositions.bind(
        f"bus:{another.seq}", admission=1, turn_id="turn", native_id="b" * 32, text=source
    )
    dispositions.record(
        "human-quote", seq=None, owner="worker", admission=1, target="worker", text=source
    )
    dispositions.bind("human-quote", admission=1, turn_id="turn", native_id="c" * 32, text=source)
    result = comms.repair_input_routing()
    assert result["eligible"] == 1 and result["skipped"] == 1


def test_cli_previews_by_default(tmp_path, capsys):
    comms, _, _, _, _ = fixture(tmp_path)
    args = ["--root", str(comms.root), "repair-input-routing"]
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["dry_run"] is True
    assert not comms.transcript_routes.database_path.exists()
    assert main([*args, "--apply"]) == 0
    assert json.loads(capsys.readouterr().out)["repaired"] == 1
