"""Commander authority follows provider declarations, not thread/model spelling."""
from dataclasses import replace
import importlib

import pytest

from agent_comms.commander.config import CommanderConfig
from agent_comms.errors import RelationViolationError
from agent_comms.coordination_errors import IdentityConflict
from agent_comms.native_arguments import NativeArguments, NoSelectedModel, SelectedModel


def test_no_selected_model_is_an_explicit_provider_case():
    assert NativeArguments(()).with_model(None).provider_type is NoSelectedModel
    assert NoSelectedModel.may_command


def test_unknown_provider_is_refused():
    with pytest.raises(ValueError, match="Unknown ModelProvider"):
        NativeArguments(()).with_model("openai-codxe/typo").provider_type


def test_incomplete_model_cannot_become_the_no_model_case():
    with pytest.raises(IdentityConflict, match="configured provider/model"):
        NativeArguments(()).with_model("malformed-model")
