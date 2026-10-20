"""Launch selection has one grammar, then typed consumers and native validation."""

import ast
from pathlib import Path


import pytest
from agent_comms.pi_vocabulary import HighThinkingLevel, NoThinkingOverride, OffThinkingLevel

from agent_comms.native_arguments import (
    InheritedArguments,
    NamedOption,
    NativeArguments,
    OneShotArgument,
    OptionArgument,
    SelectedModel,
    ValueArgument,
)
from agent_comms.thread_model import DeclaredModel, UndeclaredModel


@pytest.mark.parametrize("selection", [None, "", "model"])
def test_selected_model_requires_a_qualified_configuration(selection):
    from agent_comms.coordination_errors import IdentityConflict

    with pytest.raises(IdentityConflict):
        SelectedModel.decode(selection)


@pytest.mark.parametrize("selection", ["/model", "provider/"])
def test_selected_model_refuses_an_incomplete_configuration(selection):
    from agent_comms.coordination_errors import IdentityConflict

    with pytest.raises(IdentityConflict):
        SelectedModel.decode(selection)


def test_selected_model_owns_qualification_and_native_arguments():
    from dataclasses import FrozenInstanceError

    model = SelectedModel.decode("provider/organization/model")
    assert model == SelectedModel("provider", "organization/model")
    assert model.qualified == "provider/organization/model"
    assert model.argv == ("--provider", "provider", "--model", "organization/model")
    with pytest.raises(FrozenInstanceError):
        model.provider = "different"


def test_unconfigured_launch_has_no_provider_or_model_policy():
    arguments = InheritedArguments().configured({})
    assert arguments.argv == ()
    assert arguments.model == UndeclaredModel()
    assert arguments.thinking is NoThinkingOverride
    assert InheritedArguments().configured({
        NativeArguments.arguments_key: "--provider user --model selected",
    }).model == DeclaredModel("user/selected")


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
    assert arguments.model == DeclaredModel("original/old/model")
    assert arguments.thinking is HighThinkingLevel
    selected = arguments.with_model("response-local/fixture").with_thinking(OffThinkingLevel)
    assert selected.model == DeclaredModel("response-local/fixture") and selected.thinking is OffThinkingLevel
    assert selected.rpc() == (
        "--offline",
        "--extension",
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
    assert selected.with_thinking(NoThinkingOverride).thinking is NoThinkingOverride
    assert arguments.model == DeclaredModel("original/old/model")
    assert NativeArguments.parse(("--model", "local/fixture")).model == DeclaredModel("local/fixture")


@pytest.mark.parametrize("argv", [("--thinking", "unknown"), ("--thinking=unknown",)])
def test_unknown_thinking_fails_at_argument_parse(argv):
    with pytest.raises(ValueError):
        NativeArguments.parse(argv)


def test_option_declaration_extends_consumption_rendering_and_mode_validation():
    class FixtureValueArgument(NamedOption, ValueArgument):
        def validate_rpc(self):
            if self.value != "allowed":
                raise ValueError("fixture denied")

    arguments = NativeArguments.parse(("--fixture-value=allowed", "--mode=rpc"))
    assert arguments.value(FixtureValueArgument) == "allowed"
    assert arguments.rpc() == ("--fixture-value", "allowed", "--mode", "rpc")
    with pytest.raises(ValueError):
        NativeArguments.parse(("--fixture-value=denied",)).rpc()
    for member in OptionArgument.members_with(OneShotArgument):
        with pytest.raises(ValueError) as declared:
            member().validate_rpc()
        for flag in (value for value in (member.flag(), member.short) if value is not None):
            with pytest.raises(ValueError):
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


