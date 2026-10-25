"""Recurring problems must replace their duplicated fact with a declaration."""

import pytest

from agent_comms.change_report import FileLine
from agent_comms.slop import DeclarationDerivation, Derivation


@pytest.mark.parametrize('mechanism', [
    member for member in map(Derivation.member_named, Derivation.names())
    if issubclass(member, DeclarationDerivation)
])
def test_declaration_derivations_require_another_file(mechanism, tmp_path):
    where = FileLine('broken.py', 1)
    assert mechanism.declaration_elsewhere(FileLine('owner.py', 1), where, tmp_path)
    assert not mechanism.declaration_elsewhere(FileLine(str(tmp_path / 'broken.py'), 9), where, tmp_path)




def test_recurrence_matches_the_enclosing_declaration_not_the_file(tmp_path):
    (tmp_path / 'rules.py').write_text(
        'LIMIT = 1\n'
        '\n'
        'class First:\n'
        '    def check(self):\n'
        '        return 1\n'
        '\n'
        'class Second:\n'
        '    def check(self):\n'
        '        return 2\n'
    )
    second = FileLine('rules.py', 9).declaration(tmp_path)
    assert second.lines == range(8, 10)
    assert second.holds(FileLine('rules.py', 8), tmp_path)
    assert not second.holds(FileLine('rules.py', 5), tmp_path)
    assert not second.holds(FileLine('other.py', 9), tmp_path)
    module = FileLine('rules.py', 1).declaration(tmp_path)
    assert module.holds(FileLine('rules.py', 5), tmp_path)
