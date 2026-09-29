"""Launch selection has one grammar, then typed consumers and native validation."""

import ast
from pathlib import Path

import pytest

from agent_comms.native_arguments import (
    NamedOption,
    NativeArguments,
    OneShotArgument,
    OptionArgument,
    ValueArgument,
)


def test_selection_replaces_all_occurrences_preserving_unowned_native_arguments():
    arguments = NativeArguments.parse(
        (
            "--provider",
            "original",
            "--model=old/model",
            "--thinking",
            "low",
            "--thinking=high",
            "--offline",
            "-e",
            "extension.mjs",
        )
    )
    assert arguments.model == "original/old/model"
    assert arguments.thinking == "high"
    selected = arguments.with_model("response-local/fixture").with_thinking("off")
    assert selected.model == "response-local/fixture" and selected.thinking == "off"
    assert selected.rpc() == (
        "--offline",
        "-e",
        "extension.mjs",
        "--provider",
        "response-local",
        "--model",
        "fixture",
        "--thinking",
        "off",
        "--mode",
        "rpc",
    )
    assert selected.with_thinking(None).thinking is None
    assert arguments.model == "original/old/model"
    assert NativeArguments.parse(("--model", "local/fixture")).model == "local/fixture"


def test_option_declaration_extends_consumption_rendering_and_mode_validation():
    class FixtureValueArgument(NamedOption, ValueArgument):
        def validate_rpc(self):
            if self.value != "allowed":
                raise ValueError("fixture denied")

    arguments = NativeArguments.parse(("--fixture-value=allowed", "--mode=rpc"))
    assert arguments.value(FixtureValueArgument) == "allowed"
    assert arguments.rpc() == ("--fixture-value", "allowed", "--mode", "rpc")
    with pytest.raises(ValueError, match="fixture denied"):
        NativeArguments.parse(("--fixture-value=denied",)).rpc()
    for member in OptionArgument.members_with(OneShotArgument):
        for flag in (member.flag(), member.short):
            with pytest.raises(ValueError, match="one-shot"):
                NativeArguments.parse((flag,)).rpc()
    for mode in (("--mode", "text"), ("--mode=json",), ("--mode",)):
        with pytest.raises(ValueError):
            NativeArguments.parse(mode).rpc()


def test_backend_cannot_reintroduce_independent_launch_option_readers():
    package = Path(__file__).parents[1] / "src/agent_comms"
    removed = {
        "configured_model",
        "configured_thinking_level",
        "args_for_model",
        "args_for_thinking_level",
        "rpc_arguments",
    }
    for name in ("backend.py", "native_pi.py"):
        tree = ast.parse((package / name).read_text())
        assert not any(
            isinstance(node, ast.FunctionDef) and node.name in removed for node in ast.walk(tree)
        )
