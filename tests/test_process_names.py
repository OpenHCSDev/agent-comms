from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest

from agent_comms.execution.child_process import (
    ForkedProcessPool,
    ProcessName,
    SpawnedHelper,
)
from agent_comms.execution.remote_core_process import RemoteReadProcess
from agent_comms.transport.remote_core import RemoteCoreAccess


def _current_comm() -> tuple[int, str]:
    return os.getpid(), Path("/proc/self/comm").read_text().strip()


def _role_process(module: str, declaration: str) -> subprocess.Popen[str]:
    script = (
        f"from {module} import {declaration}; "
        f"{declaration}.publish(); "
        "import os,time; print(os.getpid(), flush=True); time.sleep(30)"
    )
    return subprocess.Popen(
        (sys.executable, "-c", script),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _assert_role_process(module: str, declaration: str, expected: str) -> None:
    process = _role_process(module, declaration)
    try:
        assert process.stdout is not None
        pid = int(process.stdout.readline())
        assert Path(f"/proc/{pid}/comm").read_text().strip() == expected
    finally:
        process.terminate()
        process.wait(timeout=10)


def test_every_persistent_python_role_publishes_its_real_kernel_name() -> None:
    _assert_role_process(
        "agent_comms.execution.worker.__main__", "CORE_WORKER_PROCESS", "ac-core-worker",
    )
    _assert_role_process("agent_comms.execution.acp", "ACP_PROCESS", "ac-acp")
    _assert_role_process(
        "agent_comms.cli_bridges.model_agent_client",
        "MODEL_AGENT_PROCESS",
        "ac-model-agent",
    )

    with RemoteReadProcess(
        cast(RemoteCoreAccess, None), SpawnedHelper(), max_workers=2,
    ) as reads:
        read_result = reads.submit(_current_comm).result(timeout=30)
    assert read_result[1] == "ac-core-read"

    with ForkedProcessPool(2, ProcessName("ac-test-pool")) as workers:
        result = workers.submit(_current_comm).result(timeout=10)
    assert result[1] == "ac-test-pool"


@pytest.mark.parametrize("value", ("", "sixteen-byte-name", "bad\nname", "bad\0name"))
def test_process_name_rejects_values_the_kernel_cannot_publish_exactly(value: str) -> None:
    with pytest.raises(ValueError):
        ProcessName(value)
