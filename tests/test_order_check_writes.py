"""Saved check commands are data; revision policy is enforced when writing new checks."""

import pytest

from agent_comms.field_codec import FieldCodec
from agent_comms.goals import CountCheckResult, OrderCheck
from agent_comms.work_branches import WorkBranches


@pytest.mark.parametrize('command', ['git checkout main', 'git -C /repo switch work',
                                   'true && git checkout HEAD',
                                   'echo first\ngit checkout main',
                                   'echo first\n\ngit switch main'])
def test_saved_checks_decode_but_new_checks_refuse_revision_selection(command):
    saved = OrderCheck('.', command)
    assert FieldCodec.decode(OrderCheck, FieldCodec.encode(saved)) == saved
    with pytest.raises(ValueError):
        saved.require_writable()
    with pytest.raises(ValueError):
        OrderCheck.parse(f'. :: {command} -> 0')


def test_multiline_checks_keep_quoted_newlines_and_command_boundaries():
    command = "printf 'first\\nsecond'\n\ngrep -c obsolete owner.py"
    check = OrderCheck.parse(f'. :: {command} -> 0')
    assert check is not None and check.command == command
    assert [words for words in check.simple_commands(command) if words] == [
        ['printf', 'first\\nsecond'], ['grep', '-c', 'obsolete', 'owner.py'],
    ]


@pytest.mark.parametrize('command,expected', [('grep -c . owner.py', 3), ('printf missing', 0)])
def test_acceptance_refuses_passing_and_unmeasurable_checks(source_repository, tmp_path, command, expected):
    check = OrderCheck(str(source_repository.checkout), command, CountCheckResult(expected))
    with pytest.raises(ValueError):
        check.require_acceptance(WorkBranches.main, tmp_path / 'checks')
    assert not tuple((tmp_path / 'checks').iterdir())


