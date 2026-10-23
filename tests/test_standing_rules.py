import json
import time
from dataclasses import replace


from agent_comms.session_file import RegistrySessionLocation
from agent_comms.todos import (DirectiveText)
from agent_comms.threads import Thread



def lines(comms, member, commander="lead"):
    return [found.line() for found in member.violations(comms, commander)]


def working(comms, name, created):
    """A unit whose native session records a turn now."""
    from datetime import UTC, datetime
    stamp = datetime.now(UTC).isoformat()
    path = comms.root / f"{name}-session.jsonl"
    rows = [dict(type="session", id=f"{name}-session", version=3, timestamp=stamp),
            dict(type="message", id=f"{name}-input", timestamp=stamp,
                 message=dict(role="user", content="Repair the owner",
                              timestamp=time.time() * 1000))]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    path.chmod(0o600)
    if name in comms.registry.all_threads():
        comms.registry.register(replace(comms.registry.require(name), session_file=RegistrySessionLocation.from_path(str(path), comms.root)))
    else:
        comms.registry.declare(Thread(
            name, frozenset(), str(comms.root), session_file=RegistrySessionLocation.from_path(str(path), tmp_path), created_at=created))


def test_plain_messages_need_no_order_labels():
    assert DirectiveText.refused("Yes, go ahead.") == ()


