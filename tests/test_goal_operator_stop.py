"""Install's actual process stop preserves a fresh autonomous continuation."""

from pathlib import Path

from goal_stop_journey import stopped_goal_journey


async def test_install_stop_preserves_goal_and_next_turn(
    tmp_path_factory, native_runtime_package, monkeypatch,
):
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[1] / "src"))
    result = await stopped_goal_journey(
        tmp_path_factory.getbasetemp() / "journey", native_runtime_package)
    assert result["child_exited"]
    assert result["provider_posts"] >= 2
    assert result["continued_turn"] != result["stopped_turn"]
    assert result["failed_turn_observations"] == 0
