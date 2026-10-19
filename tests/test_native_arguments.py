"""Launch selection has one grammar, then typed consumers and native validation."""

import ast
import re
from pathlib import Path

from native_proof_cases import read_proof_rows

import pytest
from agent_comms.pi_vocabulary import HighThinkingLevel, NoThinkingOverride, OffThinkingLevel

from agent_comms.native_arguments import (
    NamedOption,
    NativeArguments,
    OneShotArgument,
    OptionArgument,
    SelectedModel,
    ValueArgument,
)


@pytest.mark.parametrize("selection", [None, "", "model"])
def test_selected_model_requires_a_qualified_configuration(selection):
    from agent_comms.coordination_errors import IdentityConflict

    with pytest.raises(IdentityConflict, match="^Selected owner has no configured provider/model$"):
        SelectedModel.decode(selection)


@pytest.mark.parametrize("selection", ["/model", "provider/"])
def test_selected_model_refuses_an_incomplete_configuration(selection):
    from agent_comms.coordination_errors import IdentityConflict

    with pytest.raises(IdentityConflict, match="^Selected owner's configured provider/model is incomplete$"):
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
    arguments = NativeArguments.configured(environment={})
    assert arguments.argv == ()
    assert arguments.model is None
    assert arguments.thinking is NoThinkingOverride
    assert NativeArguments.configured(environment={
        NativeArguments.arguments_key: "--provider user --model selected",
    }).model == "user/selected"


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
    assert arguments.thinking is HighThinkingLevel
    selected = arguments.with_model("response-local/fixture").with_thinking(OffThinkingLevel)
    assert selected.model == "response-local/fixture" and selected.thinking is OffThinkingLevel
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
    assert arguments.model == "original/old/model"
    assert NativeArguments.parse(("--model", "local/fixture")).model == "local/fixture"


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
    with pytest.raises(ValueError, match="fixture denied"):
        NativeArguments.parse(("--fixture-value=denied",)).rpc()
    for member in OptionArgument.members_with(OneShotArgument):
        with pytest.raises(ValueError) as declared:
            member().validate_rpc()
        for flag in (value for value in (member.flag(), member.short) if value is not None):
            with pytest.raises(ValueError, match=re.escape(str(declared.value))):
                NativeArguments.parse((flag,)).rpc()
    for mode in (("--mode", "text"), ("--mode=json",), ("--mode",)):
        with pytest.raises(ValueError):
            NativeArguments.parse(mode).rpc()


def test_managed_and_ephemeral_launches_own_distinct_extension_arguments(
    tmp_path, native_runtime_package, selected_model
):
    import shlex

    from agent_comms.native_pi import NativePiRpcLaunch

    options = dict(package=native_runtime_package, worktree=tmp_path, root=tmp_path)
    ephemeral = NativePiRpcLaunch.catalog("pi", (), **options)
    managed = NativePiRpcLaunch.managed("pi", (), **options, directory=tmp_path)
    assert "--no-extensions" in ephemeral.argv and "-e" not in ephemeral.argv
    assert managed.argv.index("--no-extensions") < managed.argv.index("-e")
    assert managed.retained_managed("pi", (), worktree=tmp_path, root=tmp_path,
        environment=None, session_file=None, directory=tmp_path) == managed
    print("P576_EPHEMERAL=" + shlex.join(ephemeral.argv))
    print("P576_MANAGED=" + shlex.join(managed.argv))


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


async def test_saved_native_selection_survives_acp_load_and_one_new_prompt(
    native_backend, monkeypatch
):
    import asyncio
    from dataclasses import replace

    from acp.agent.router import build_agent_router

    from agent_comms.comms import Comms
    from delivery_owner_fixture import canonical_agent

    native = native_backend
    assert (await native.run("Retain this launch-selection history"))[-1].ok
    await native.persistent.close()
    history = native.session.read_bytes()
    proof_path = native.session.with_suffix(native.session.suffix + ".input-proof")
    proof = read_proof_rows(native.session)
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "response-local/fixture")
    owner = canonical_agent(
        Comms(native.root),
        auto_wake=False,
        runtime_enabled=True,
        agent_args=[
            "--provider=response-local",
            "--model=fixture",
            "--thinking=low",
            "--thinking=off",
            "--offline",
            "--no-extensions",
            "--no-skills",
            "--no-context-files",
            "--no-prompt-templates",
            "--no-tools",
        ],
    )
    router = build_agent_router(owner)
    try:
        async with asyncio.timeout(25):
            created = await router(
                "session/new", {"cwd": str(native.project), "mcpServers": []}, False
            )
            sid = created.session_id
            thread = owner._comms.registry.require(sid)
            assert thread.model == "response-local/fixture" and thread.thinking_level is OffThinkingLevel
            # Seed the representative saved-thread binding through the existing store.
            owner._comms.registry.register(replace(thread, auto_title_pending=False))
            owner._comms.threads.attach_session((attachment_owner := owner._comms.registry.require(sid)), str(native.session), process=attachment_owner.process_identity)
            await router(
                "session/load",
                {"cwd": str(native.project), "sessionId": sid, "mcpServers": []},
                False,
            )
            thread = owner._comms.registry.require(sid)
            state = await owner.turns.backend_for(sid, thread).prepare(thread)
            assert state.model.display_name == "response-local/fixture"
            assert (
                native.session.read_bytes() == history and read_proof_rows(native.session) == proof
            )
            child = owner.turns.persistent_backends[sid].child_state.child
            assert child.attestation.state.thinking_level is OffThinkingLevel
            response = await router(
                "session/prompt",
                {
                    "sessionId": sid,
                    "prompt": [{"type": "text", "text": "One new launch-selection input"}],
                },
                False,
            )
            await asyncio.gather(*tuple(owner.turns.turn_tasks.values()))
            assert response.stop_reason == "end_turn"
            assert native.session.read_bytes().startswith(history)
            assert all(row in read_proof_rows(native.session) for row in proof)
            assert len(native.saved_inputs()) == 2 and native.provider.posts == 2
            assert not owner._comms.registry.require(sid).executing
    finally:
        await owner.shutdown()
    assert not owner.turns.persistent_backends
