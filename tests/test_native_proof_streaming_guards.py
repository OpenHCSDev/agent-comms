"""Prevent restoring the retired lifetime proof quota or body snapshot."""
from pathlib import Path


def test_native_proof_recovery_has_one_streaming_implementation():
    root = Path(__file__).resolve().parents[1]
    startup = (root / "experiments/pi-context-proof/pi-0.85.1-native-input.patch").read_text()
    startup = startup[startup.index("+    *_nativeProofRows"):startup.index("+    _claimNativeInput")]
    assert "readFileSync" not in startup
    assert ".getEntries()" not in startup
    assert "_nativeInputClaims.set" not in startup
    assert "raw.length" not in startup
    for retired in ("patch-native-proof-headroom.py", "patch-native-input-recovery.py"):
        assert not (root / "stack" / retired).exists()
        assert retired not in (root / "stack/bin/prepare-pi-native").read_text()
