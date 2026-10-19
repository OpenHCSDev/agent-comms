"""Commander authority follows provider declarations, not thread/model spelling."""
from dataclasses import replace
import importlib

import pytest

from agent_comms.commander.config import CommanderConfig
from agent_comms.errors import RelationViolationError
from agent_comms.native_arguments import SelectedModel


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
            rules.RulesVerb.parse([*argv, "--as", "worker"], rules.__doc__).apply()
    assert comms.registry.store.path.read_bytes() == before
    assert not (order.CONFIG.state / "forks" / "worker.beat").exists()
    refused_home = tmp_path / "refused-registration"
    config = order.CONFIG.model_copy(update={"commander": "worker"})
    with pytest.raises(RelationViolationError, match="worker.*openai-codex/friendly-renamed-model"):
        config.write(refused_home)
    assert not refused_home.exists()
    assert comms.registry.require("worker").tags == frozenset({"unit", "installer"})
    rules.RulesVerb.parse(["stale", "--as", "lead"], rules.__doc__).apply()
    assert capsys.readouterr().out
    assert SelectedModel.decode("anthropic/codex-named-but-not-codex-provider").provider_type.may_command
    assert not SelectedModel.decode("openai-codex/any-model-id").provider_type.may_command
