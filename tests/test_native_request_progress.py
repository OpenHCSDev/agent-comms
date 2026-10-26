"""Real local transports/projector, followed by the persisted Core wire decoder."""

import json
import subprocess
from pathlib import Path

from agent_comms.field_codec import FieldCodec
from agent_comms.request_progress import RequestProgress, UnreportedIteratorTiming


def test_native_iterator_custody_is_preserved_in_request_records(pinned_native, tmp_path):
    receipt = tmp_path / "request-progress.json"
    result = subprocess.run(
        ["node", str(Path(__file__).with_name("native_request_progress_contract.mjs")),
         str(pinned_native), str(receipt)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    controls = json.loads(receipt.read_text())["controls"]
    for control in controls:
        for record in control["points"]:
            decoded = FieldCodec.decode(RequestProgress, record)
            encoded = FieldCodec.encode(decoded)
            if "iteratorTiming" in record:
                assert encoded["iteratorTiming"] == record["iteratorTiming"]
            else:
                assert isinstance(decoded.iterator_timing, UnreportedIteratorTiming)
                assert "iteratorTiming" not in encoded
        if first_delta := control.get("firstDelta"):
            decoded = FieldCodec.decode(RequestProgress, first_delta)
            assert FieldCodec.encode(decoded)["iteratorTiming"] == first_delta["iteratorTiming"]
    # Historical observations remain readable, without manufacturing measurements.
    original = dict(controls[0]["points"][-1])
    del original["iteratorTiming"]
    assert isinstance(FieldCodec.decode(RequestProgress, original).iterator_timing,
                      UnreportedIteratorTiming)
    measured_zero = dict(controls[0]["points"][-1])
    measured_zero["iteratorTiming"] = dict(measured_zero["iteratorTiming"],
                                           iteratorAwaitMs=0, consumerYieldMs=0)
    assert FieldCodec.encode(FieldCodec.decode(RequestProgress, measured_zero))["iteratorTiming"] == measured_zero["iteratorTiming"]
