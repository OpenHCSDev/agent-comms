"""Every commander verb parses its documented command line from its declared option() fields."""
import importlib
import shlex

import pytest

DOCUMENTED = [
    ("order", "send worker 23:59 order.txt --clean --reassign 'missed two deadlines' --fork f1",
     {"unit": "worker", "due": "23:59", "body_file": "order.txt", "clean": True, "mechanical": False,
      "reassign": "missed two deadlines", "disk": False, "fork": "f1"}),
    ("order", "strike worker --why late --caught 'lead at install'",
     {"unit": "worker", "why": "late", "caught": "lead at install", "fork": None}),
    ("order", "strike worker --why late", {"unit": "worker", "why": "late", "caught": "lead at review"}),
    ("order", "done P1 --sha abc123", {"pid": "P1", "sha": "abc123"}),
    ("order", "say worker,#comms note.txt", {"units": "worker,#comms", "body_file": "note.txt"}),
    ("order", "problem 'the text' --to worker --due 23:59 --order order.txt --disk",
     {"text": "the text", "to": "worker", "due": "23:59", "order": "order.txt", "disk": True}),
    ("order", "close P1 --works seen --artifact seen.png", {"pid": "P1", "works": "seen", "artifact": "seen.png"}),
    ("order", "hunt a.py:1 --why why --sent abc", {"where": "a.py:1", "why": "why", "sent": "abc"}),
    ("order", "next-hunt", {}),
    ("order", "board", {}),
    ("order", "overdue", {}),
    ("judge", "show worker", {"thread": "worker"}),
    ("judge", "show", {"thread": None}),
    ("judge", "alarm", {}),
    ("judge", "ok 0123456789:ab --serves 'the words' --line one --line two --why why --fork f1",
     {"ids": ("0123456789:ab",), "serves": "the words", "lines": ("one", "two"), "why": "why", "fork": "f1"}),
    ("judge", "back 0123456789:ab --msg m1 --line one --why why --roast r --fix f --slop state"
              " --fact 'four words of fact' --at a.py:1 --derive delete-copy --by b.py:2",
     {"ids": ("0123456789:ab",), "msg": "m1", "lines": ("one",), "roast": "r", "fix": "f", "slop_kind": "state",
      "fact": "four words of fact", "at": "a.py:1", "derive": "delete-copy", "by": "b.py:2"}),
]


@pytest.mark.parametrize("tool,line,fields", DOCUMENTED)
def test_every_verb_parses_its_documented_command_line(commander_script, tool, line, fields):
    order, _ = commander_script
    family = (order.OrderVerb if tool == "order"
              else importlib.reload(importlib.import_module("agent_comms.commander.judge")).JudgeVerb)
    verb = family.parse(shlex.split(line), "")
    assert verb.declared_name == line.split()[0]
    assert {name: getattr(verb, name) for name in fields} == fields
