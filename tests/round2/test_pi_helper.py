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
    fence = tmp_path / "dist/agent-comms-import-fence.mjs"
    fence.parent.mkdir()
    fence.write_text('globalThis.fixtureFence = true;\n')
    assert await NewCaseHelper.run(
        UpperRequest(str(tmp_path), "declared"), cwd=tmp_path
    ) == UpperResult("DECLARED")
    with pytest.raises(PiHelperError, match="invalid evidence"):
        await NewCaseHelper.run(UpperRequest(str(tmp_path), "declared", True), cwd=tmp_path)


async def test_helper_cannot_run_without_native_import_fence(tmp_path):
    with pytest.raises(PiHelperError, match="failed"):
        await NewCaseHelper.run(UpperRequest(str(tmp_path), "declared"), cwd=tmp_path)
