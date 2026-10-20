"""Recurring problems must replace their duplicated fact with a declaration."""

import pytest

from agent_comms.change_report import FileLine
from agent_comms.todos import (
    DeclarationDerivation, Derivation,
)


@pytest.mark.parametrize('mechanism', [
    member for member in map(Derivation.member_named, Derivation.names())
    if issubclass(member, DeclarationDerivation)
])
def test_declaration_derivations_require_another_file(mechanism, tmp_path):
    where = FileLine('broken.py', 1)
    assert mechanism.declaration_elsewhere(FileLine('owner.py', 1), where, tmp_path)
    assert not mechanism.declaration_elsewhere(FileLine(str(tmp_path / 'broken.py'), 9), where, tmp_path)


