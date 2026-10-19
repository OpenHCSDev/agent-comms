"""The watcher decodes each Pi session line through Core's native entry family once.

Edits are derived from native turn calls, not journaled by the watcher (7e8a0ef8d)."""
import collections
import importlib
import json

from agent_comms.native_tool_call import CodingCall
from agent_comms.native_tool_call import SelectedToolDenied
from agent_comms.todos import StorageStrikeTodo
from test_commander_problem_orders import create


def test_pi_calls_through_the_watcher_digest(tool_watch, commander):
    comms, _ = commander
    calls = [("edit", {"path": "a.py", "edits": [{"oldText": "old  text", "newText": "new"}]}),
             ("write", {"path": "b.py", "content": "body\nline"}),
             ("bash", {"command": "ls -la"})]
    line = json.dumps({"type": "message", "id": "m1", "timestamp": "2026-10-18T22:00:00.000Z",
                       "message": {"role": "assistant", "stopReason": "toolUse", "content": [
                           {"type": "toolCall", "id": f"c{index}", "name": name, "arguments": arguments}
                           for index, (name, arguments) in enumerate(calls)]}})
    turns = collections.defaultdict(list)
    tool_watch.follow(comms.registry, turns, "worker", "session.jsonl", 0, f"{line}\n".encode())
    assert turns["worker"] == ["edit a.py: -old text +new", "write b.py: body line", "bash ls -la"]


def test_coordination_call_digests_its_arguments():
    call = CodingCall.decode('coordination-1', 'comms_threads', {})
    assert call.digest() == 'comms_threads {}'


def test_a_line_core_refuses_is_reported_with_its_file_and_offset(tool_watch, commander, capsys):
    comms, _ = commander
    whole = json.dumps({"type": "message", "id": "m1", "timestamp": "2026-10-18T22:00:00.000Z",
                        "message": {"role": "user", "content": "hello"}})
    data = f'{whole}\n{{"type": "message"\n'.encode()
    tool_watch.follow(comms.registry, collections.defaultdict(list), "worker", "session.jsonl", 7, data)
    assert capsys.readouterr().out.startswith(f"REFUSED session.jsonl@{7 + len(whole) + 1}: ")


def test_storage_watch_judges_only_core_units(tool_watch, commander, monkeypatch):
    comms, _ = commander
    create(commander, 'P1')
    judged = []
    monkeypatch.setattr(comms.registry.__class__, 'decide_storage',
                        lambda self, thread, effects: judged.append(thread))
    call = CodingCall.decode('write-1', 'write', {'path': 'a.py', 'content': 'body'})
    tool_watch.enforce_storage(comms.registry, 'lead', call)
    assert judged == []
    tool_watch.enforce_storage(comms.registry, 'worker', call)
    assert judged == ['worker']


def test_storage_watch_reports_refused_call_without_judging(tool_watch, commander, monkeypatch, capsys):
    comms, _ = commander
    create(commander, 'P1')
    judged = []
    monkeypatch.setattr(comms.registry.__class__, 'decide_storage',
                        lambda self, thread, effects: judged.append(thread))
    call = CodingCall.decode('refused-1', 'write', {'path': 'a.py'})
    tool_watch.enforce_storage(comms.registry, 'worker', call)
    assert judged == []
    assert capsys.readouterr().out == ''
    assert call.digest() == 'REFUSED write: Missing required fields for WriteTool: content'


def test_watcher_decodes_each_call_once_and_logs_its_owned_digest(tool_watch, commander, monkeypatch):
    from agent_comms.tool_declaration import ToolDeclaration

    comms, _ = commander
    seen = []
    parse = ToolDeclaration.parse_arguments.__func__

    def counted(cls, arguments):
        seen.append(cls.declared_name)
        return parse(cls, arguments)

    monkeypatch.setattr(ToolDeclaration, 'parse_arguments', classmethod(counted))
    calls = [('bad', {'path': 'a.py'}), ('good', {'path': 'b.py', 'content': 'body'})]
    line = json.dumps({'type': 'message', 'id': 'digest-once', 'timestamp': '2026-10-19T07:50:00.000Z',
                       'message': {'role': 'assistant', 'stopReason': 'toolUse', 'content': [
                           {'type': 'toolCall', 'id': id, 'name': 'write', 'arguments': args}
                           for id, args in calls]}})
    turns = collections.defaultdict(list)
    tool_watch.follow(comms.registry, turns, 'worker', 'session.jsonl', 0, (line + '\n').encode())
    assert seen == ['write', 'write']
    assert turns['worker'] == ['REFUSED write: Missing required fields for WriteTool: content', 'write b.py: body']


def test_storage_violation_is_struck_once_across_watch_restart(tool_watch, commander, monkeypatch):
    comms, _ = commander
    before = create(commander, 'P1')
    announcements = []
    monkeypatch.setattr(tool_watch.order, 'on_record', lambda *args: announcements.append(args))

    def deny(self, thread, effects):
        raise SelectedToolDenied('The write creates a file outside the declared scratch directory')

    monkeypatch.setattr(comms.registry.__class__, 'decide_storage', deny)
    call = CodingCall.decode('write-1', 'write', {'path': 'a.py', 'content': 'body'})
    tool_watch.enforce_storage(comms.registry, 'worker', call)
    tool_watch.enforce_storage(comms.registry, 'worker', call)
    restarted = importlib.reload(tool_watch)
    restarted.enforce_storage(comms.registry, 'worker', call)
    after = comms.goals.todos.get('P1')
    assert after.revision == before.revision + 1
    assert len(after.live_strikes('worker')) == 1
    assert isinstance(after.live_strikes('worker')[0], StorageStrikeTodo)
    assert len(announcements) == 1
