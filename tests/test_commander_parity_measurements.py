"""Comparison invariants; configured live-owner proof is the --verify application run."""

from dataclasses import replace

from agent_comms.commander_parity.measurements import RuleComparison, StorageComparison
from agent_comms.commander_parity.rows import UnitTimeParityRow


def test_measurement_family_requires_native_calls_exact_delivery_and_no_private_write():
    matched = RuleComparison("owner", ("delivered",), ("delivered",), 1, True)
    assert matched.passed
    for changed in (
        replace(matched, calls=0),
        replace(matched, calls=2),
        replace(matched, current=("different",)),
        replace(matched, native=()),
        replace(matched, private_state_unchanged=False),
    ):
        assert not changed.passed
        assert not StorageComparison(changed, matched).passed
        assert not StorageComparison(matched, changed).passed
    assert StorageComparison(matched, matched).passed
    missing = UnitTimeParityRow.verify()
    assert not missing.passed
    assert UnitTimeParityRow.native in missing.describe()
