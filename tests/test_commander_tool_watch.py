"""The watcher decodes each Pi session line through Core's native entry family once.

Edits are derived from native turn calls, not journaled by the watcher (7e8a0ef8d)."""
import collections
import importlib
import json

import pytest

from agent_comms.native_tool_call import CodingCall
from agent_comms.pi_payloads import AssistantMessage, StorageDenialToolDetails, ToolCallContent
from test_commander_problem_orders import create


def test_pi_calls_through_the_watcher_digest(tool_watch, commander, commander_script):
    comms, _ = commander
    calls = [("edit", {"path": "a.py", "edits": [{"oldText": "old  text", "newText": "new"}]}),
             ("write", {"path": "b.py", "content": "body\nline"}),
             ("bash", {"command": "ls -la"})]
    line = json.dumps({"type": "message", "id": "m1", "timestamp": "2026-10-18T22:00:00.000Z",
                       "message": {"role": "assistant", "stopReason": "toolUse", "content": [
                           {"type": "toolCall", "id": f"c{index}", "name": name, "arguments": arguments}
                           for index, (name, arguments) in enumerate(calls)]}})
    turns = collections.defaultdict(list)
    tool_watch.follow(commander_script[0], turns, "worker", f"{line}\n".encode())
    assert turns["worker"] == ["edit a.py: -old text +new", "write b.py: body line", "bash ls -la"]


def test_every_coding_family_uses_the_shared_storage_decision(commander):
    from agent_comms.storage_rules import WriteStorageEffect

    comms, _ = commander
    thread = comms.registry.require('worker')
    path = thread.storage_roots.scratch / 'checked.py'
    calls = (
        CodingCall.decode('bash', 'bash', {'command': 'ls -la'}),
        CodingCall.decode('read', 'read', {'path': str(path)}),
        CodingCall.decode('edit', 'edit', {'path': str(path), 'edits': [
            {'oldText': 'old', 'newText': 'new'}]}),
        CodingCall.decode('write', 'write', {'path': str(path), 'content': 'body'}),
        CodingCall.decode('request', 'comms_threads', {}),
        CodingCall.decode('invalid', 'write', {'path': str(path)}),
    )
    effects = tuple(call.decide_storage(comms.registry, thread.name) for call in calls)
    write = (WriteStorageEffect(path),)
    assert effects == ((), (), write, write, (), ())


def test_coordination_call_digests_its_arguments():
    call = CodingCall.decode('coordination-1', 'comms_threads', {})
    assert call.digest() == 'comms_threads {}'


def test_a_malformed_line_raises_instead_of_skipping_history(tool_watch, commander, commander_script):
    comms, _ = commander
    whole = json.dumps({"type": "message", "id": "m1", "timestamp": "2026-10-18T22:00:00.000Z",
                        "message": {"role": "user", "content": "hello"}})
    data = f'{whole}\n{{"type": "message"\n'.encode()
    with pytest.raises(json.JSONDecodeError):
        tool_watch.follow(commander_script[0], collections.defaultdict(list), "worker", data)


def test_storage_watch_judges_only_core_units(tool_watch, commander, commander_script, monkeypatch):
    comms, _ = commander
    create(commander, 'P1')
    judged = []
    monkeypatch.setattr(comms.registry.__class__, 'decide_storage',
                        lambda self, thread, effects: judged.append(thread))
    denial = StorageDenialToolDetails('StorageRule: foreign scratch destination was refused before execution')
    tool_watch.enforce_storage(commander_script[0], 'lead', 'write-1', denial)
    assert judged == []
    tool_watch.enforce_storage(commander_script[0], 'worker', 'write-1', denial)
    assert judged == []


def test_storage_watch_reports_refused_call_without_judging(tool_watch, commander, commander_script, monkeypatch, capsys):
    comms, _ = commander
    create(commander, 'P1')
    judged = []
    monkeypatch.setattr(comms.registry.__class__, 'decide_storage',
                        lambda self, thread, effects: judged.append(thread))
    call = CodingCall.decode('refused-1', 'write', {'path': 'a.py'})
    tool_watch.walk(commander_script[0], collections.defaultdict(list), 'worker',
                    AssistantMessage(content=(ToolCallContent(
                        'refused-1', 'write', {'path': 'a.py'}),)))
    assert judged == []
    assert capsys.readouterr().out == ''
    assert call.digest() == 'REFUSED write: content: Missing required fields for WriteTool: content; fix: supply {"type": "string"}'


def test_watcher_decodes_each_call_once_and_logs_its_owned_digest(tool_watch, commander, commander_script, monkeypatch):
    from agent_comms.tool_declaration import ToolDeclaration

    comms, _ = commander
    seen = []
    parse = ToolDeclaration.parse_arguments.__func__

    def counted(cls, arguments, *, call_id):
        seen.append(cls.declared_name)
        return parse(cls, arguments, call_id=call_id)

    monkeypatch.setattr(ToolDeclaration, 'parse_arguments', classmethod(counted))
    calls = [('bad', {'path': 'a.py'}), ('good', {'path': 'b.py', 'content': 'body'})]
    line = json.dumps({'type': 'message', 'id': 'digest-once', 'timestamp': '2026-10-19T07:50:00.000Z',
                       'message': {'role': 'assistant', 'stopReason': 'toolUse', 'content': [
                           {'type': 'toolCall', 'id': id, 'name': 'write', 'arguments': args}
                           for id, args in calls]}})
    turns = collections.defaultdict(list)
    tool_watch.follow(commander_script[0], turns, 'worker', (line + '\n').encode())
    assert seen == ['write', 'write']
    assert turns['worker'] == ['REFUSED write: content: Missing required fields for WriteTool: content; fix: supply {"type": "string"}', 'write b.py: body']


def test_storage_violation_keeps_orders_and_strikes_across_watch_restart(
        tool_watch, commander, commander_script, monkeypatch, capsys):
    comms, _ = commander
    before = create(commander, 'P1')
    announcements = []
    monkeypatch.setattr(type(commander_script[0]), 'on_record', lambda *args: announcements.append(args))

    def forbidden(*args):
        raise AssertionError('watcher re-ran storage preflight')

    monkeypatch.setattr(comms.registry.__class__, 'decide_storage', forbidden)
    denial = StorageDenialToolDetails('StorageRule: the write creates a file outside the declared scratch directory')
    line = (json.dumps({'type': 'message', 'id': 'storage-denial',
                        'timestamp': '2026-10-19T22:50:00Z', 'message': {
                            'role': 'toolResult', 'toolCallId': 'write-1', 'toolName': 'write',
                            'isError': True, 'details': denial.to_wire(), 'content': []}}) + '\n').encode()
    turns = collections.defaultdict(list)
    tool_watch.follow(commander_script[0], turns, 'worker', line)
    tool_watch.follow(commander_script[0], turns, 'worker', line)
    restarted = importlib.reload(tool_watch)
    restarted.follow(commander_script[0], turns, 'worker', line)
    after = comms.goals.todos.get('P1')
    assert after == before
    assert after.live_strikes('worker') == ()
    assert announcements == []
    assert capsys.readouterr().out.count('VIOLATION storage worker:') == 3
