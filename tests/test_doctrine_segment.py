"""The maintenance doctrine segment derives from its one declared source file."""

from __future__ import annotations

from pathlib import Path

from agent_comms.turn_context import ContextSegment, CoordinationSegment, InstructionFile, NextContextTurn, TurnContext
from test_private_bus_checkpoint import _root

# The provider measures the real tokens (about four bytes per token); the
# declared file's byte size is the bound the reviewer controls when appending
# cases, so the injection stays under 400 tokens.
FILE_BYTE_BUDGET = 1600


def _doctrine():
    from agent_comms.context_segments.doctrine import DoctrineSegment

    return DoctrineSegment


def test_the_segment_derives_its_text_from_the_declared_file(tmp_path: Path):
    captured = _doctrine().capture()
    instruction = InstructionFile.read("doctrine.md")
    assert captured.instruction == instruction
    assert captured.provenance == (instruction.source,)
    assert captured.text() == instruction.content
    assert captured.values() == {}
    # The declared source stays the single owner of the injected bytes.
    assert len(instruction.content.encode()) <= FILE_BYTE_BUDGET


def test_every_owner_context_injects_the_doctrine_before_coordination(tmp_path: Path):
    comms, _ = _root(tmp_path)
    for name in ("Alice", "Bob", "outsider"):
        context = TurnContext.for_owner(
            comms.registry.require(name), NextContextTurn(), "original task", (), ()
        )
        kinds = tuple(type(segment) for segment in context.segments)
        assert kinds[0] is _doctrine()
        assert kinds[1] is CoordinationSegment
        assert kinds[2].public_title() == "User Input"
        assert context.segments[0].text() == InstructionFile.read("doctrine.md").content
        assert context.render().text.startswith(context.segments[0].text())


def test_the_doctrine_is_a_declared_family_member():
    doctrine = _doctrine()
    assert doctrine.declared_name == "doctrine"
    assert doctrine in ContextSegment.members_with(ContextSegment)
    assert doctrine.public_title() == "Doctrine"
