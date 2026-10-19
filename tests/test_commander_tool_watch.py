"""The watcher decodes each Pi session line through Core's native entry family once.

Edits are derived from native turn calls, not journaled by the watcher (7e8a0ef8d)."""
import collections
import json


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


def test_a_line_core_refuses_is_reported_with_its_file_and_offset(tool_watch, commander, capsys):
    comms, _ = commander
    whole = json.dumps({"type": "message", "id": "m1", "timestamp": "2026-10-18T22:00:00.000Z",
                        "message": {"role": "user", "content": "hello"}})
    data = f'{whole}\n{{"type": "message"\n'.encode()
    tool_watch.follow(comms.registry, collections.defaultdict(list), "worker", "session.jsonl", 7, data)
    assert capsys.readouterr().out.startswith(f"REFUSED session.jsonl@{7 + len(whole) + 1}: ")
