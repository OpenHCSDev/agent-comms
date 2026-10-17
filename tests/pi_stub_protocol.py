"""Shared JSON-lines handshake for fake Pi child processes."""

import json
import sys


def send(event):
    print(json.dumps(event), flush=True)


def read_state():
    """Read a state request without deciding how a fault fixture answers it."""
    request = json.loads(sys.stdin.readline())
    assert request["type"] == "get_state"
    return request


def state_response(request, data):
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
        request = json.loads(sys.stdin.readline())
        if request["type"] == "prompt":
            return request
        send({"id": request["id"], "type": "response",
              "command": request["type"], "success": True})
