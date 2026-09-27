"""The withdrawn PID-namespace opt-in must refuse before any child dispatch."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agent_comms import native_pi, native_pi_retirement


def test_native_turn_opt_in_denies_before_package_or_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("retirement prototype reached a native launch")

    monkeypatch.setattr(native_pi, "prepare_native_pi_rpc_launch", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    monkeypatch.setattr(native_pi, "spawn_retired_child", forbidden)
    with pytest.raises(native_pi.NativePiUnavailable, match="disabled before dispatch"):
        asyncio.run(
            native_pi.run_native_pi_turn(
                Path("missing-package"),
                input_id="a" * 32,
                prompt="not sent",
                worktree=Path("missing-worktree"),
                session_dir=Path("missing-session-dir"),
                retirement_identity=object(),  # type: ignore[arg-type] - no validation after deny
            )
        )


def test_direct_namespace_spawn_denies_before_session_or_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("retirement prototype reached a native launch")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    monkeypatch.setattr(native_pi_retirement, "_session_identity", forbidden)
    with pytest.raises(
        native_pi_retirement.RetirementUnavailable, match="disabled before dispatch"
    ):
        asyncio.run(
            native_pi_retirement.spawn_retired_child(
                object(), object(), "a" * 32, "not sent"  # type: ignore[arg-type]
            )
        )
