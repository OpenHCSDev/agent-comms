"""Prepared command declarations, without applying an operation or starting Pi."""
import asyncio

from agent_comms.cli_commands import (
    ChoiceTargetField,
    DeleteTagCliCommand,
    RenameTagCliCommand,
)


def test_prepared_field_kind_owns_choice_metadata():
    plain, = asyncio.run(RenameTagCliCommand.for_tag('p74').prepare_fields())
    original, = DeleteTagCliCommand.for_tag('p74').editable_fields
    finite, = asyncio.run(DeleteTagCliCommand.for_tag('p74').prepare_fields())

    assert plain.name == 'new_name'
    assert plain.editor_hints(lambda declaration: declaration) == ()
    assert isinstance(finite, ChoiceTargetField)
    assert finite.editor_hints(lambda declaration: declaration) == (finite,)
    assert finite.choices == original.choices
    assert finite.editor_default == original.editor_default
