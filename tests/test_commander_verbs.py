"""Every commander verb parses its documented command line from its declared option() fields."""
import importlib
import shlex

import pytest

from agent_comms.recorded_time import RecordedEventTime, UnrecordedEventTime
from agent_comms.tool_caller import NoThreadCaller, ThreadCaller

DOCUMENTED = [
    ("order", "send worker order.txt --due 23:59 --clean --reassign 'missed two deadlines' --fork f1",
     {"unit": "worker", "due": RecordedEventTime.from_clock('23:59'), "body_file": "order.txt", "clean": True,
      "reassign": "missed two deadlines", "fork": ThreadCaller("f1")}),
    ("order", "send worker order.txt", {"unit": "worker", "body_file": "order.txt", "due": UnrecordedEventTime()}),
    ("order", "problem order.json --to worker --where file.py:1",
     {"to": "worker", "order": "order.json", "where": "file.py:1", "due": UnrecordedEventTime()}),
    ("order", "strike worker --why 'P1 late' --caught 'lead at install'",
     {"unit": "worker", "why": "P1 late", "caught": "lead at install", "fork": NoThreadCaller()}),
    ("order", "strike worker --why 'P1 late'",
     {"unit": "worker", "why": "P1 late", "caught": "review"}),
    ("order", "done worker m1 --sha abc123 --seen proof.txt", {"unit": "worker", "message": "m1", "sha": "abc123", "seen": "proof.txt"}),
    ("order", "withdraw P68 --why 'duplicate of P67' --superseded-by P67",
     {"pid": "P68", "why": "duplicate of P67", "superseded_by": "P67"}),
    ("order", "withdraw P68 --why 'sent in error'", {"pid": "P68", "superseded_by": None}),
    ("order", "check P1 --check 'repo :: cat n -> 1' --why 'counted the wrong file'",
     {"pid": "P1", "check": "repo :: cat n -> 1", "why": "counted the wrong file"}),
    ("order", "say worker,#comms note.txt", {"units": "worker,#comms", "body_file": "note.txt"}),
    ("order", "problem order.json --to worker --due 23:59 --where file.py:1",
     {"to": "worker", "due": RecordedEventTime.from_clock('23:59'), "order": "order.json", "where": "file.py:1"}),
    ("order", "unstrike P1 --why 'recorded in error' --at 12:46",
     {"pid": "P1", "why": "recorded in error", "at": "12:46"}),
    ("order", "fire worker --why 'late twice'", {"unit": "worker", "why": "late twice"}),
    ("order", "close P1 --works seen --artifact seen.png", {"pid": "P1", "works": "seen", "artifact": "seen.png"}),
    ("order", "hunt a.py:1 --why why --sent abc",
     {"where": "a.py:1", "why": "why", "sent": "abc"}),
    ("order", "hunt a.py:1 --why why",
     {"where": "a.py:1", "why": "why", "sent": ""}),
    ("order", "next-hunt", {}),
    ("order", "board", {}),
    ("request", "add 'finish the commander plan'", {"text": "finish the commander plan"}),
    ("request", "complete r1 --why 'phases delivered'", {"id": "r1", "why": "phases delivered"}),
    ("request", "list", {}),
    ("judge", "show worker", {"thread": "worker"}),
    ("judge", "show", {}),
    ("judge", "alarm", {}),
    ("judge", "ok 0123456789:ab --msg m1 --serves 'the words' --line one --line two --why why --fork f1",
     {"ids": ("0123456789:ab",), "msg": "m1", "serves": "the words", "lines": ("one", "two"), "why": "why",
      "fork": ThreadCaller("f1")}),
    ("judge", "ok 0123456789:ab --msg m1 --serves 'the words' --line one --why why --no-owner proof",
     {"serves": "the words", "no_owner": "proof"}),
    ("judge", "back 0123456789:ab --msg m1 --line one --why why --roast r --fix f --slop IDEN-5"
              " --fact 'four words of fact' --at a.py:1 --derive delete-copy --by b.py:2",
     {"ids": ("0123456789:ab",), "msg": "m1", "lines": ("one",), "roast": "r", "fix": "f", "pattern": "IDEN-5",
      "fact": "four words of fact", "at": "a.py:1", "derive": "delete-copy", "by": "b.py:2"}),
]


@pytest.mark.parametrize("tool,line,fields", DOCUMENTED)
def test_every_verb_parses_its_documented_command_line(commander_script, tool, line, fields):
    order = importlib.import_module("agent_comms.commander.order")
    module = (order if tool == "order"
              else importlib.reload(importlib.import_module(f"agent_comms.commander.{tool}")))
    family = getattr(module, f"{tool.capitalize()}Verb")
    verb = family.parse(shlex.split(line), "")
    assert verb.declared_name == line.split()[0]
    assert {name: getattr(verb, name) for name in fields} == fields
