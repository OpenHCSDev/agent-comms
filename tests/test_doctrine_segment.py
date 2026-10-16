"""Session rules have one source and never accumulate in turn history."""
import time
import pytest

from agent_comms.native_pi import NativePiRpcLaunch
from agent_comms.native_errors import NativePiUnavailable
from agent_comms.launch_runtime import RestartEnvironment
from agent_comms.native_project import UnmanagedNativeProject
from agent_comms.turn_context import (
    ContextSegment, CoordinationSegment, InstructionFile, NextContextTurn,
    SessionCoordinationSegment, TurnContext, UserInputSegment,
)
from agent_comms.context_segments.doctrine import DoctrineSegment
from test_private_bus_checkpoint import _root


def test_session_rules_derive_from_their_declared_sources(tmp_path):
    comms, _ = _root(tmp_path)
    owner = comms.registry.require("Alice")
    doctrine, coordination = TurnContext.session_segments(owner)
    assert doctrine.instruction == InstructionFile.read("doctrine.md")
    assert doctrine.provenance == (doctrine.instruction.source,)
    assert doctrine.text() == doctrine.instruction.content
    assert isinstance(coordination, SessionCoordinationSegment)
    assert "thread 'Alice'" in coordination.text()
    assert repr(owner.worktree) in coordination.text()
    assert "Confirmed worked cases" not in doctrine.text()
    assert "delivered automatically" in coordination.text()


def test_owner_turns_do_not_repeat_session_rules(tmp_path):
    comms, _ = _root(tmp_path)
    for name in ("Alice", "Bob", "outsider"):
        context = TurnContext.for_owner(
            comms.registry.require(name), NextContextTurn(), "ping", (), (),
            now=time.time(),
        )
        assert [type(segment) for segment in context.segments] == [
            CoordinationSegment, UserInputSegment,
        ]
        text = context.render().text
        assert "Maintenance results" not in text
        assert "overrides inherited" not in text
        assert "Project:" not in text
        assert text.endswith("ping")
        assert len(text.encode()) < 150


def test_native_launch_takes_required_typed_context_not_environment(tmp_path):
    comms, _ = _root(tmp_path)
    owner = comms.registry.require("Alice")
    segments = TurnContext.session_segments(owner)
    configuration = RestartEnvironment.inherit({})
    arguments = (tmp_path / "cli.js", (), tmp_path, {}, configuration, comms.runtime)
    project = UnmanagedNativeProject()
    with pytest.raises(TypeError, match="session_context"):
        NativePiRpcLaunch.bootstrap(*arguments, project=project)
    with pytest.raises(NativePiUnavailable, match="nonempty session context"):
        NativePiRpcLaunch.bootstrap(*arguments, session_context=(), project=project)
    argv, environment = NativePiRpcLaunch.bootstrap(*arguments, session_context=segments, project=project)
    assert argv.count("--append-system-prompt") == 2
    assert argv[-1] == TurnContext.render_segments(segments).text
    assert "thread 'Alice'" in argv[-1]
    assert "AGENT_COMMS_SESSION_CONTEXT" not in environment


def test_doctrine_is_a_declared_family_member():
    assert DoctrineSegment.declared_name == "doctrine"
    assert DoctrineSegment in ContextSegment.members_with(ContextSegment)
