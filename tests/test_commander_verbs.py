"""Every commander verb parses its documented command line from its declared option() fields."""
import importlib
import re
import shlex

import pytest

DOCUMENTED = [
    ("order", "send worker 23:59 order.txt --request r1 --clean --reassign 'missed two deadlines' --fork f1",
     {"unit": "worker", "due": "23:59", "body_file": "order.txt", "request": "r1", "clean": True,
      "mechanical": False, "reassign": "missed two deadlines", "fork": "f1"}),
    ("order", "strike worker P1 --why late --caught 'lead at install'",
     {"unit": "worker", "pid": "P1", "why": "late", "caught": "lead at install", "fork": None}),
    ("order", "strike worker P1 --why late",
     {"unit": "worker", "pid": "P1", "why": "late", "caught": "lead at review"}),
    ("order", "done P1 --sha abc123", {"pid": "P1", "sha": "abc123"}),
    ("order", "withdraw P68 --why 'duplicate of P67' --superseded-by P67",
     {"pid": "P68", "why": "duplicate of P67", "superseded_by": "P67"}),
    ("order", "withdraw P68 --why 'sent in error'", {"pid": "P68", "superseded_by": None}),
    ("order", "check P1 --check 'repo :: cat n -> 1' --why 'counted the wrong file'",
     {"pid": "P1", "check": "repo :: cat n -> 1", "why": "counted the wrong file"}),
    ("order", "say worker,#comms note.txt", {"units": "worker,#comms", "body_file": "note.txt"}),
    ("order", "problem 'the text' --to worker --due 23:59 --order order.txt --request r1",
     {"text": "the text", "to": "worker", "due": "23:59", "order": "order.txt", "request": "r1"}),
    ("order", "unstrike P1 --why 'recorded in error' --at 12:46",
     {"pid": "P1", "why": "recorded in error", "at": "12:46"}),
    ("order", "fire worker --why 'late twice'", {"unit": "worker", "why": "late twice"}),
    ("order", "close P1 --works seen --artifact seen.png", {"pid": "P1", "works": "seen", "artifact": "seen.png"}),
    ("order", "hunt a.py:1 P1 --why why --sent abc",
     {"where": "a.py:1", "pid": "P1", "why": "why", "sent": "abc", "clean": False}),
    ("order", "hunt a.py:1 P1 --why why --clean",
     {"where": "a.py:1", "pid": "P1", "why": "why", "sent": None, "clean": True}),
    ("order", "next-hunt", {}),
    ("order", "board", {}),
    ("order", "overdue", {}),
    ("request", "add 'finish the commander plan'", {"text": "finish the commander plan"}),
    ("request", "complete r1 --why 'phases delivered'", {"id": "r1", "why": "phases delivered"}),
    ("request", "list", {}),
    ("judge", "show-unit worker", {"thread": "worker"}),
    ("judge", "show", {}),
    ("judge", "alarm", {}),
    ("judge", "ok 0123456789:ab --msg m1 --serves 'the words' --line one --line two --why why --fork f1",
     {"ids": ("0123456789:ab",), "msg": "m1", "serves": "the words", "lines": ("one", "two"), "why": "why",
      "fork": "f1"}),
    ("judge", "ok-unowned 0123456789:ab --msg m1 --serves 'the words' --line one --why why --no-owner proof",
     {"serves": "the words", "no_owner": "proof"}),
    ("judge", "back 0123456789:ab --msg m1 --line one --why why --roast r --fix f --slop state"
              " --fact 'four words of fact' --at a.py:1 --derive delete-copy --by b.py:2",
     {"ids": ("0123456789:ab",), "msg": "m1", "lines": ("one",), "roast": "r", "fix": "f", "slop_kind": "state",
      "fact": "four words of fact", "at": "a.py:1", "derive": "delete-copy", "by": "b.py:2"}),
]


@pytest.mark.parametrize("tool,line,fields", DOCUMENTED)
def test_every_verb_parses_its_documented_command_line(commander_script, tool, line, fields):
    order, _ = commander_script
    module = (order if tool == "order"
              else importlib.reload(importlib.import_module(f"agent_comms.commander.{tool}")))
    family = getattr(module, f"{tool.capitalize()}Verb")
    verb = family.parse(shlex.split(line), "")
    assert verb.declared_name == line.split()[0]
    assert {name: getattr(verb, name) for name in fields} == fields


def test_the_hunt_prompt_prints_a_command_line_the_hunt_parser_accepts(commander_script):
    order, _ = commander_script
    printed = order.HuntVerb.due("a.py").split("then ", 1)[1]
    tool, verb, *usage = re.sub(r"\[[^]]*\]", "", printed).split()
    assert (tool, verb) == ("order", "hunt")
    required = " ".join(usage)
    for outcome in re.search(r"\((.*?)\)", required).group(1).split(" | "):
        line = re.sub(r"\(.*?\)", outcome, required)
        argv = [verb, *(re.sub(r"^[A-Z:]+$", "x", token) for token in line.split())]
        parsed = order.OrderVerb.parse(argv, "")
        assert type(parsed) is order.HuntVerb and getattr(parsed, outcome.split()[0].removeprefix("--"))
