"""Recurring problems must replace their duplicated fact with a declaration."""

import pytest

from agent_comms.change_report import FileLine
from agent_comms.slop import DeclarationDerivation, Derivation


@pytest.fixture
def source(tmp_path):
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
    (tmp_path / 'other.py').write_text('class Owner:\n    pass\n')
    return tmp_path


@pytest.mark.parametrize('mechanism', Derivation.members_with(DeclarationDerivation))
def test_declaration_derivations_require_another_declaration(mechanism, source):
    where = FileLine('rules.py', 9)
    assert mechanism.declaration_elsewhere(FileLine('rules.py', 3), where, source)
    assert mechanism.declaration_elsewhere(FileLine('other.py', 1), where, source)
    assert not mechanism.declaration_elsewhere(FileLine('rules.py', 8), where, source)
    assert not mechanism.declaration_elsewhere(
        FileLine(str(source / 'rules.py'), 9), where, source)


def test_recurrence_matches_the_enclosing_declaration_not_the_file(source):
    second = FileLine('rules.py', 9).declaration(source)
    assert second.at == FileLine(str(source / 'rules.py'), 8)
    assert second == FileLine(str(source / 'rules.py'), 8).declaration(source)
    assert second.lines == range(8, 10)
    assert second.holds(FileLine('rules.py', 8), source)
    assert not second.holds(FileLine('rules.py', 5), source)
    assert not second.holds(FileLine('other.py', 9), source)
    module = FileLine('rules.py', 1).declaration(source)
    assert module.holds(FileLine('rules.py', 5), source)
