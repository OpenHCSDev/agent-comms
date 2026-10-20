"""Forecast acceptance uses the shared copied-bus lifecycle, real Git and native owners."""

import json
import sys
from pathlib import Path

from agent_comms.commander.config import CommanderConfig
from agent_comms.commander_parity.prediction import PredictionCase
from agent_comms.comms import Comms
from agent_comms.field_codec import FieldCodec
from agent_comms.storage_rules import StorageRule
from agent_comms.thread_execution import ExternalThreadExecution
from agent_comms.todos import TodoStore


def test_order_reviewed_twice_then_closed_retains_last_prediction_on_a_copied_bus(
    tmp_path, monkeypatch,
):
    source = Comms(tmp_path / "source")
    source.messaging.initialize_private_initial_protocol()
    policy = StorageRule(scratch_base=tmp_path / "scratch", checkout_base=tmp_path / "wt")
    (source.root / "storage.json").write_text(json.dumps(FieldCodec.encode(policy)))
    source = Comms(source.root)  # Registration captures the declared policy on construction.
    for name in ("lead", "worker"):
        source.registry.declare(source.registry.new_thread(
            name, frozenset({"comms"}), str(tmp_path), execution=ExternalThreadExecution,
        ))
    CommanderConfig(
        commander="lead", root=source.root, cli=Path(sys.executable).parent / "agent-comms",
        state=tmp_path / "state", board=tmp_path / "board.md",
        narrative=tmp_path / "narrative.md", repos=(), worktrees=tmp_path / "wt",
        scratch=tmp_path / "scratch", disk=tmp_path,
    ).write(tmp_path)
    # Boundary configuration, not a replacement of any owner or its return value.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AGENT_COMMS_THREAD", "worker")
    result = PredictionCase.read()
    assert result.passed, result.describe()
    assert not TodoStore(source.root).path.exists()  # Original has no order or forecast writes.
