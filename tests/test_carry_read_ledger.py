"""The read ledger carry keeps every read from the earlier text-keyed ledger."""

import importlib.util
import json
from pathlib import Path

from agent_comms.read_basis import Conversation, ReadCursor
from agent_comms.read_ledger import ReadLedger
from agent_comms.thread_identity import RecordedThreadIncarnation

TOOL = Path(__file__).resolve().parents[1] / "tools/cutover/carry_read_ledger.py"
spec = importlib.util.spec_from_file_location("carry_read_ledger", TOOL)
carry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(carry)


def earlier_ledger(root: Path) -> None:
    channel = {"target": "#comms", "participants": []}
    direct = {"target": "", "participants": [["alice", 2.5], ["user", 1.5]]}
    (root / "read_ledger.json").write_text(json.dumps({
        "messages": {
            json.dumps(["user", 1.5, channel], separators=(",", ":")): [1, 2, 4],
            json.dumps(["user", 1.5, direct], separators=(",", ":")): [3],
        },
        "bus_identity": [66308, 6861023],
        "transcripts": {json.dumps(["user", "/sessions/a.jsonl", 6860966]): 4881511},
        "notice": None,
    }))


def test_carry_keeps_every_read_and_runs_once(tmp_path):
    earlier_ledger(tmp_path)
    assert carry.carry_locked(tmp_path) == {
        "message_reads": 2, "transcript_reads": 1, "carried": True, "applied": False,
    }
    assert carry.carry_locked(tmp_path, apply=True)["carried"]

    document = ReadLedger.at(tmp_path).read()
    user = RecordedThreadIncarnation("user", 1.5)
    channel = Conversation("#comms")
    direct = Conversation("", (RecordedThreadIncarnation("alice", 2.5), user))
    assert document.messages == {user: {channel: ReadCursor(2, (4,)), direct: ReadCursor(0, (3,))}}
    assert document.bus_identity == (66308, 6861023)
    assert document.transcripts == {("user", "/sessions/a.jsonl", 6860966): 4881511}
    assert document.notice is None

    assert carry.carry_locked(tmp_path, apply=True) == {
        "message_reads": 0, "transcript_reads": 0, "carried": False, "applied": True,
    }
