"""Shared JSON-lines handshake for fake Pi child processes."""

import json
import sys


_pending = []
_state_data = None


def read_request(line=None):
    """Decode and record one request actually received from Core."""
    request = json.loads(sys.stdin.readline() if line is None else line)
    _pending.append(request)
    return request


def _take_response(event):
    for index, request in enumerate(_pending):
        if request["type"] == event["command"] and (
            "id" not in event or request.get("id") == event["id"]
        ):
            return _pending.pop(index)


def _write_event(event):
    print(json.dumps(event), flush=True)


def send(event, *, write=_write_event):
    if event.get("type") == "response":
        request = _take_response(event)
        if event.get("command") == "get_session_stats" and "id" not in event:
            # Old fixture sequences specify statistics, not a fabricated reply ID.
            # Wait for the real query and answer its preceding state query too.
            while request is None:
                incoming = read_request()
                if incoming["type"] == "get_state":
                    send(state_response(incoming, _state_data), write=write)
                elif incoming["type"] != "get_session_stats":
                    raise ValueError("Statistics fixture received another command")
                request = _take_response(event)
            event = {**event, "id": request.get("id")}
        # Explicit foreign IDs are deliberate fault injections, never repaired.
    write(event)


def read_state():
    """Read a state request without deciding how a fault fixture answers it."""
    request = read_request()
    assert request["type"] == "get_state"
    return request


def state_response(request, data):
    global _state_data
    _state_data = data
    return {
        "id": request["id"],
        "type": "response",
        "command": "get_state",
        "success": True,
        "data": data,
    }


def answer_state(data):
    send(state_response(read_state(), data))


def next_prompt():
    while True:
        request = read_request()
        if request["type"] == "prompt":
            return request
        send({"id": request["id"], "type": "response",
              "command": request["type"], "success": True})
