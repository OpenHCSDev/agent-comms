"""One-time terminal repair changes only its exact retained claim set."""

from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.claim_admission import publish_selected_resource_claim
from agent_comms.coordination_errors import RecoveryBlocked
from agent_comms.envelope_claim_transitions import ExistingFileClaim
from test_claim_bus_read import selected_turn, settle_failed
from tools.cutover.release_terminal_attempt_claims import apply, inspect


def test_terminal_claim_repair_refuses_live_attempt_then_releases_exact_terminal_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    with selected_turn() as (comms, store, selected, resource):
        first = publish_selected_resource_claim(
            comms, store, selected, "Alice", ExistingFileClaim(resource)
        )
        other = resource.parent / "other.py"
        other.write_text("unchanged\n")
        second = publish_selected_resource_claim(
            comms,
            store,
            replace(selected, operation_id="1" * 32),
            "Alice",
            ExistingFileClaim(other),
        )
        with pytest.raises(RecoveryBlocked, match="terminal attempt"):
            inspect(comms.root, "execution")

        # Reproduce the historical producer: terminal SQL committed before the
        # release behavior existed, leaving both exact generations on the bus.
        session_type = type(store.session)
        monkeypatch.setattr(session_type, "release_resources", lambda *_: None)
        settle_failed(store)
        monkeypatch.undo()
        assert store.snapshots.get("execution").attempt.lifecycle.terminal
        assert set(comms.bus.log.claim_projection()) == {first.resource, second.resource}

        expected = inspect(comms.root, "execution")
        assert {(row.resource, row.generation) for row in expected.claims} == {
            (first.resource, first.generation),
            (second.resource, second.generation),
        }
        result = apply(comms.root, expected)
        assert result["released"] == 2
        assert result["sql_and_registry_unchanged"] is True
        assert dict(comms.bus.log.claim_projection()) == {}
