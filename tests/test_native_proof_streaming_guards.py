"""Prevent restoring retired proof quotas, scanners or generation mirrors.

Behavior is covered through actual installed native growth/crash cases in
``test_native_proof_recovery.py``; this guard protects deletion of the old paths.
"""

import json
from pathlib import Path

from agent_comms.native_pi import NativeContextJournal


def test_retired_native_proof_mechanisms_stay_deleted():
    root = Path(__file__).resolve().parents[1]
    startup = (root / "experiments/pi-context-proof/pi-0.85.1-native-input.patch").read_text()
    for retired in ("_nativeProofRows", "_nativeRequestGeneration"):
        assert retired not in startup
    for retired in ("patch-native-proof-headroom.py", "patch-native-input-recovery.py"):
        assert not (root / "stack" / retired).exists()
        assert retired not in (root / "stack/bin/prepare-pi-native").read_text()
    assert not (root / "stack/test-native-proof-streaming.mjs").exists()
    assert not (root / "tools/cutover/native_proof_journal.py").exists()


def test_generated_native_schema_is_exactly_the_existing_declaration():
    root = Path(__file__).resolve().parents[1]
    generated = (
        (root / "stack/native-proof-schema.mjs").read_text().split(" = ", 1)[1].removesuffix(";\n")
    )
    assert json.loads(generated) == NativeContextJournal.native_contract()
