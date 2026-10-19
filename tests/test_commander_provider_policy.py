"""Commander authority follows provider declarations, not thread/model spelling."""
from dataclasses import replace
import importlib

import pytest

from agent_comms.commander.config import CommanderConfig
from agent_comms.errors import RelationViolationError
from agent_comms.coordination_errors import IdentityConflict
from agent_comms.native_arguments import NativeArguments, NoSelectedModel, SelectedModel


def test_provider_declaration_controls_verbs_and_registration(commander, commander_script, tmp_path, capsys):
    comms, _ = commander
    order, _ = commander_script
    worker = replace(comms.registry.require("worker"), model="openai-codex/friendly-renamed-model",
                     tags=frozenset({"unit", "installer"}))
    comms.registry.register(worker)
    rules = importlib.import_module("agent_comms.commander.rules")
    rules = importlib.reload(rules)
    before = comms.registry.store.path.read_bytes()
    for argv in (("stale",), ("review",), ("answer", "1", "This answer must never be recorded for the refused provider.")):
        with pytest.raises(RelationViolationError, match="worker.*openai-codex/friendly-renamed-model"):
            rules.RulesVerb.parse([*argv, "--fork", "worker"], rules.__doc__, order).apply()
    assert comms.registry.store.path.read_bytes() == before
    assert not (order.state / "forks" / "worker.beat").exists()
    refused_home = tmp_path / "refused-registration"
    config = order.model_copy(update={"commander": "worker"})
    with pytest.raises(RelationViolationError, match="worker.*openai-codex/friendly-renamed-model"):
        config.write(refused_home)
    assert not refused_home.exists()
    assert comms.registry.require("worker").tags == frozenset({"unit", "installer"})
    rules.RulesVerb.parse(["stale", "--fork", "lead"], rules.__doc__, order).apply()
    assert capsys.readouterr().out
    assert SelectedModel.decode("anthropic/codex-named-but-not-codex-provider").provider_type.may_command
    assert not SelectedModel.decode("openai-codex/any-model-id").provider_type.may_command


def test_no_selected_model_is_an_explicit_provider_case():
    assert NativeArguments(()).with_model(None).provider_type is NoSelectedModel
    assert NoSelectedModel.may_command


def test_unknown_provider_is_refused():
    with pytest.raises(ValueError, match="Unknown ModelProvider"):
        NativeArguments(()).with_model("openai-codxe/typo").provider_type


def test_incomplete_model_cannot_become_the_no_model_case():
    with pytest.raises(IdentityConflict, match="configured provider/model"):
        NativeArguments(()).with_model("malformed-model")
