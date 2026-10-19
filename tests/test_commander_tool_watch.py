"""The watcher decodes each Pi tool call through Core's tool family once."""
import collections
import json


def test_pi_calls_through_the_watcher_digest_and_journal(tool_watch, commander):
    comms, _ = commander
    calls = [("edit", {"path": "a.py", "edits": [{"oldText": "old  text", "newText": "new"}]}),
             ("write", {"path": "b.py", "content": "body\nline"}),
             ("bash", {"command": "ls -la"})]
    line = json.dumps({"message": {"role": "assistant", "stopReason": "toolUse", "content": [
        {"type": "toolCall", "name": name, "arguments": arguments} for name, arguments in calls]}})
    turns = collections.defaultdict(list)
    tool_watch.follow(comms.registry, turns, "worker", [line])
    assert turns["worker"] == ["edit a.py: -old text +new", "write b.py: body line", "bash ls -la"]
    edits = tool_watch.judge.Edit.recorded().values()
    assert [(edit.thread, edit.path, edit.hunks) for edit in edits] == [
        ("worker", "a.py", (tool_watch.judge.Hunk("old  text", "new"),)),
        ("worker", "b.py", (tool_watch.judge.Hunk("", "body\nline"),))]
