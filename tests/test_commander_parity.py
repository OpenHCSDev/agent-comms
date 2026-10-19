import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_comms.commander_parity_cli import ParityMatrix
from agent_comms.field_codec import FieldCodec
from agent_comms.parity_checks import CommanderParityCheck, CommanderParityReport


PLAN = Path(__file__).resolve().parents[1] / "plans/commander-native-plan.md"


def test_actual_native_current_comparisons_never_pass_unverified_rows(commander_script):
    order, _ = commander_script
    environment = {**os.environ, "PYTHONPATH": str(PLAN.parents[1] / "src")}
    completed = subprocess.run(
        (sys.executable, "-m", "agent_comms.cli", "--root", str(order.COMMS.root),
         "commander-parity-observe", "--plan", str(PLAN)),
        env=environment, capture_output=True, text=True, timeout=30,
    )
    assert completed.stdout, completed.stderr
    report = FieldCodec.decode(CommanderParityReport, json.loads(completed.stdout))
    declarations = CommanderParityCheck.members_with(CommanderParityCheck)
    assert declarations
    assert {row.check for row in report.comparisons} == set(declarations)
    observed = tuple(row for row in report.comparisons if row.observations)
    assert observed and all(row.passed for row in observed)
    assert all(row.phases == row.check.phases and row.phases for row in report.comparisons)
    assert {check.number for check in declarations} == set(ParityMatrix.read(PLAN).rows)
    assert not CommanderParityCheck.all_done(())
    complete_numbers = {row.check.number for row in report.comparisons
                        if row.coverage.complete and row.observations}
    assert report.unverified_rows == tuple(
        number for number in ParityMatrix.read(PLAN).rows if number not in complete_numbers)
    assert report.unverified_rows
    assert not report.passed
    assert report.failing_rows == ()
    assert completed.returncode == report.exit_code() == 1
    assert FieldCodec.decode(CommanderParityReport, report.to_wire()) == report


def test_matrix_numbers_are_not_titles_and_invalid_or_duplicate_numbers_are_refused(tmp_path):
    plan = tmp_path / "plan.md"
    plan.write_text("## Parity matrix\n\n| # | Capability |\n|---|---|\n"
                    "| 9 | Renamed freely |\n| 10 | Another name |\n")
    assert ParityMatrix.read(plan).rows == (9, 10)
    plan.write_text("## Parity matrix\n\n| # | Capability |\n|---|---|\n"
                    "| 9 | First |\n| 9 | Second |\n")
    with pytest.raises(ValueError, match="unique positive"):
        ParityMatrix.read(plan)
    plan.write_text("## Parity matrix\n\n| # | Capability |\n|---|---|\n| x | Invalid |\n")
    with pytest.raises(ValueError):
        ParityMatrix.read(plan)
