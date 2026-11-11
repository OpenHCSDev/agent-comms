"""Launch selection has one grammar, then typed consumers and native validation."""

import pytest

from agent_comms.execution.native_arguments import (
    InheritedArguments,
    NamedOption,
    NativeArguments,
    OneShotArgument,
    OptionArgument,
    ProviderArgument,
    SelectedModel,
    ValueArgument,
)
from agent_comms.execution.pi_vocabulary import (
    HighThinkingLevel,
    NoThinkingOverride,
    OffThinkingLevel,
)
from agent_comms.registry.thread_model import (
    DeclaredModel,
    ThreadModel,
    UndeclaredModel,
)


@pytest.mark.parametrize("selection", [None, "", "model", "/model", "provider/"])
def test_selected_model_requires_a_complete_qualified_configuration(selection):
    from agent_comms.coordination.errors import IdentityConflict

    with pytest.raises(IdentityConflict):
        SelectedModel.decode(selection)


def test_selected_model_owns_qualification_and_native_arguments():
    model = SelectedModel.decode("provider/organization/model")
    assert model == SelectedModel("provider", "organization/model")
    assert model.qualified == "provider/organization/model"
    assert model.argv == ("--provider", "provider", "--model", "organization/model")


def test_unconfigured_launch_has_no_provider_or_model_policy():
    arguments = InheritedArguments().configured({})
    assert arguments.argv == ()
    assert ThreadModel.selected_by(arguments) == UndeclaredModel()
    assert arguments.thinking is NoThinkingOverride
    assert ThreadModel.selected_by(InheritedArguments().configured({
        NativeArguments.arguments_key: "--provider user --model selected",
    })) == DeclaredModel("user/selected")


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
    assert ThreadModel.selected_by(arguments) == DeclaredModel("original/old/model")
    assert arguments.thinking is HighThinkingLevel
    selected = arguments.with_model("response-local/fixture").with_thinking(OffThinkingLevel)
    assert ThreadModel.selected_by(selected) == DeclaredModel("response-local/fixture") and selected.thinking is OffThinkingLevel
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
    assert ThreadModel.selected_by(arguments) == DeclaredModel("original/old/model")
    assert ThreadModel.selected_by(NativeArguments.parse(("--model", "local/fixture"))) == DeclaredModel("local/fixture")


def test_entries_answer_selection_and_replacement_for_themselves():
    arguments = NativeArguments.parse(
        ("--provider", "p1", "--model", "m1", "--offline", "--thinking", "low", "--provider", "p2")
    )
    assert arguments.provider == ProviderArgument("p2")
    assert ThreadModel.selected_by(arguments) == DeclaredModel("p2/m1")
    assert arguments.with_model("p3/m3").argv == (
        "--offline", "--thinking", "low", "--provider", "p3", "--model", "m3",
    )
    assert arguments.with_thinking(HighThinkingLevel).argv == (
        "--provider", "p1", "--model", "m1", "--offline", "--provider", "p2", "--thinking", "high",
    )
    assert NativeArguments.parse(("--mode", "rpc", "--offline")).rpc() == (
        "--offline", "--mode", "rpc",
    )


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
    assert arguments.rpc() == ("--fixture-value", "allowed", "--mode", "rpc")
    with pytest.raises(ValueError):
        NativeArguments.parse(("--fixture-value=denied",)).rpc()
    for member in OptionArgument.members_with(OneShotArgument):
        with pytest.raises(ValueError) as declared:
            member().validate_rpc()
        for flag in (member.flag(), *member.shorts):
            with pytest.raises(ValueError):
                NativeArguments.parse((flag,)).rpc()
    for mode in (("--mode", "text"), ("--mode=json",), ("--mode",)):
        with pytest.raises(ValueError):
            NativeArguments.parse(mode).rpc()


