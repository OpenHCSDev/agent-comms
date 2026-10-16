"""Launch selection has one grammar, then typed consumers and native validation."""

import argparse
import ast
import asyncio
import os
import re
import sys
from pathlib import Path

from native_proof_cases import read_proof_rows

import pytest

from agent_comms import worker
from agent_comms.native_arguments import (
    NamedOption,
    NativeArguments,
    OneShotArgument,
    OptionArgument,
    ValueArgument,
)
from test_backend_native_lifecycle import native_backend as native_backend
from agent_comms.pi_vocabulary import OffThinkingLevel
from agent_comms.active_route import LocalRoute
from agent_comms.native_settings import NativeThreadDefaults
from agent_comms.thread_identity import (
    ThreadIncarnation, RecordedThreadIncarnation,
    DeletedThreadIncarnation, UnrecordedThreadIncarnation,
)
import dataclasses
from agent_comms.launch_runtime import LaunchRuntime


def test_unconfigured_launch_has_no_provider_or_model_policy():
    defaults = NativeThreadDefaults.from_runtime(LaunchRuntime.current())
    arguments = NativeArguments.parse(defaults.arguments)
    assert arguments.argv == ()
    assert arguments.model is None
    assert arguments.thinking is None
    selected = NativeThreadDefaults(
        binary=defaults.binary, arguments=("--provider", "user", "--model", "selected"),
    )
    assert NativeArguments.parse(selected.arguments).model == "user/selected"


def test_native_settings_cli_and_original_command_share_the_declared_codec():
    selected = NativeThreadDefaults(binary="/fixture pi", arguments=("--label", "two words"))
    assert NativeThreadDefaults.from_arguments(selected.cli_arguments, selected) == selected
    assert NativeThreadDefaults.from_arguments(("python", "-m", "agent_comms.worker",
                                               *selected.cli_arguments)) == selected
    assert NativeThreadDefaults.from_arguments((), selected) is selected
    with pytest.raises(ValueError, match="Missing required --native-settings"):
        NativeThreadDefaults.from_arguments(("python", "-m", "agent_comms.worker"))
    with pytest.raises(ValueError):
        NativeThreadDefaults.from_json('{"binary":42}')
    with pytest.raises(ValueError):
        NativeThreadDefaults.from_json('{"binary":"/fixture/pi","arguments":[42]}')


@pytest.mark.parametrize("record_type,record", [
    (NativeThreadDefaults, NativeThreadDefaults(binary="/fixture pi", arguments=("two words",))),
    (ThreadIncarnation, RecordedThreadIncarnation("fixture-owner", 1.0)),
])
def test_record_flags_share_required_default_and_strict_json_boundary(record_type, record):
    assert record_type.from_arguments(record.cli_arguments) == record
    assert record_type.from_arguments((), record) is record
    with pytest.raises(ValueError, match="Missing required"):
        record_type.from_arguments(())
    with pytest.raises(argparse.ArgumentError):
        record_type.from_arguments((record_type.value_flag, "not json"), record)
    parser = argparse.ArgumentParser()
    record_type.add_cli_arguments(parser, required=True, dest="selected")
    assert parser.parse_args(record.cli_arguments).selected == record
    with pytest.raises(SystemExit) as missing:
        parser.parse_args(())
    assert missing.value.code == 2


@pytest.mark.parametrize("incarnation", [
    DeletedThreadIncarnation("fixture-owner"), UnrecordedThreadIncarnation("fixture-owner"),
])
def test_worker_refuses_nonrecorded_identity_before_runtime_or_route(incarnation):
    # Generic transport retains all family members; the worker owns live identity admission.
    decoded = ThreadIncarnation.from_arguments(incarnation.cli_arguments)
    assert decoded == incarnation
    with pytest.raises(ValueError, match="recorded"):
        asyncio.run(worker.run(NativeThreadDefaults(binary="/fixture/pi"), decoded, None))


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
        with pytest.raises(ValueError) as declared:
            member().validate_rpc()
        for flag in (value for value in (member.flag(), member.short) if value is not None):
            with pytest.raises(ValueError, match=re.escape(str(declared.value))):
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
    selected_comms = Comms(LocalRoute(native.root), runtime=LaunchRuntime.current())
    owner = canonical_agent(selected_comms, native=dataclasses.replace(selected_comms.owners.native_defaults, arguments=tuple([
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
        ])), auto_wake=False)
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
            owner._comms.threads.attach_session(sid, str(native.session))
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
            child = owner.turns.persistent_backends[sid].custody.child
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
            assert owner._comms.registry.require(sid).active_turn is None
    finally:
        await owner.shutdown()
    assert not owner.turns.persistent_backends
