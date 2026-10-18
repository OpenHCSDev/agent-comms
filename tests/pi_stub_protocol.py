"""Shared JSON-lines handshake for fake Pi child processes."""

import json
import sys


def send(event):
    print(json.dumps(event), flush=True)


def read_request(line=None):
    """Decode one request actually received from Core."""
    return json.loads(sys.stdin.readline() if line is None else line)


def read_state():
    """Read a state request without deciding how a fault fixture answers it."""
    request = read_request()
    assert request["type"] == "get_state"
    return request


def request_response(request, data):
    return {
        "id": request["id"],
        "type": "response",
        "command": request["type"],
        "success": True,
        "data": data,
    }


def answer_state(data):
    send(request_response(read_state(), data))


def answer_stats(data):
    request = read_request()
    assert request["type"] == "get_session_stats"
    send(request_response(request, data))


def next_prompt():
    while True:
        request = read_request()
        if request["type"] == "prompt":
            return request
        send({"id": request["id"], "type": "response",
              "command": request["type"], "success": True})
