"""A newly declared helper inherits actual child lifetime and strict output decoding."""

from dataclasses import dataclass
from pathlib import Path

import pytest

from agent_comms.pi_helper import PiHelper, PiHelperError


@dataclass(frozen=True)
class UpperRequest:
    package: str
    text: str
    extra: bool = False


@dataclass(frozen=True)
class UpperResult:
    answer: str


class NewCaseHelper(PiHelper):
    script = Path(__file__).parents[1] / "fixtures/pi_helpers/new_case.mjs"
    request = UpperRequest
    result = UpperResult


async def test_new_helper_inherits_real_runner_and_strict_result(tmp_path):
    assert await NewCaseHelper.run(
        UpperRequest(str(tmp_path), "declared"), cwd=tmp_path
    ) == UpperResult("DECLARED")
    with pytest.raises(PiHelperError, match="invalid evidence"):
        await NewCaseHelper.run(UpperRequest(str(tmp_path), "declared", True), cwd=tmp_path)
