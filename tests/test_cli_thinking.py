"""Stopped-thread configuration uses the session's model-specific catalog."""


from agent_comms.native_arguments import NativeArguments
from agent_comms.pi_vocabulary import OffThinkingLevel


def test_native_arguments_configured_has_one_default(monkeypatch):
    monkeypatch.delenv(NativeArguments.arguments_key, raising=False)
    monkeypatch.delenv(NativeArguments.binary_key, raising=False)
    assert NativeArguments.configured_binary() == NativeArguments.default_binary
    assert NativeArguments.configured().argv == ()
    monkeypatch.setenv(NativeArguments.binary_key, "/fixture/pi")
    monkeypatch.setenv(NativeArguments.arguments_key, '--model "fixture/model" --thinking off')
    assert NativeArguments.configured_binary() == "/fixture/pi"
    assert NativeArguments.configured().model == "fixture/model"
    assert NativeArguments.configured().thinking is OffThinkingLevel
    assert NativeArguments.configured([]).argv == ()


