"""Commander authority follows provider declarations, not thread/model spelling."""

import pytest

from agent_comms.coordination_errors import IdentityConflict
from agent_comms.native_arguments import NativeArguments, NoSelectedModel


def test_no_selected_model_is_an_explicit_provider_case():
    assert NativeArguments(()).with_model(None).provider_type is NoSelectedModel
    assert NoSelectedModel.may_command


def test_unknown_provider_is_refused():
    with pytest.raises(ValueError):
        NativeArguments(()).with_model("openai-codxe/typo").provider_type


def test_incomplete_model_cannot_become_the_no_model_case():
    with pytest.raises(IdentityConflict):
        NativeArguments(()).with_model("malformed-model")
