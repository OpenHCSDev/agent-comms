# agent-comms

IRC for agents plus humans.

A coordination wire where agent threads and human threads join the same
channels, see the same messages, and fork the same way. One shared bus, one
registry, one ledger — every client (pi, Toad, VS Code, Textual TUI, plain
CLI) is a thin adapter over the same Python core.

Every type owns exactly one concept's semantics. Instantiating a type declares
the concept: constructing a [`Thread`][agent_comms.Thread] declares a thread;
constructing a [`Message`][agent_comms.Message] declares a message. Required
relations are proved at construction time and at every operation boundary.
Unknown references raise — the system is fail-closed.

## Components

- **Core** (`agent_comms.declarations`, `agent_comms.operations`) — zero
  dependencies. Threads, messages, registry, JSONL bus, shared ledger.
- **CLI** (`agent-comms`) — JSON over stdout; the adapter surface for the pi
  extension and other process-based clients.
- **ACP server** (`agent-comms-acp`) — Agent Client Protocol agent over
  stdio, so Toad, Zed, and VS Code connect natively. Pure stdlib.
- **TUI** (`agent_comms.tui`) — Textual overview client for humans. Optional
  dep (`agent-comms[tui]`).
- **Pi extension** (`extensions/pi-agent-comms/`) — thin TypeScript shim that
  exposes `comms_send`, `comms_inbox`, `comms_threads`, and `comms_fork` as
  tool calls backed by the CLI.

## Quick start

```python
from pathlib import Path
from agent_comms import Thread, wire

comms = wire(Path("~/.agent-comms").expanduser())
comms.register(Thread(name="PR111", tags=frozenset({"base"}), worktree="/tmp/wt"))
comms.broadcast("PR111", "CI is green")
comms.inbox("fixer")
```

Humans join the same wire — register a thread with your name and read the
inbox from the TUI:

```bash
agent-comms --root ~/.agent-comms register --name tristan --worktree ~/code
agent-comms --root ~/.agent-comms inbox --thread tristan
python -m agent_comms.tui --root ~/.agent-comms --thread tristan
```

Fail-closed:

```python
comms.registry.require("nonexistent")
# UnregisteredThreadError: Thread 'nonexistent' is not registered.
```

## Development

```bash
pip install -e ".[dev]"
pytest
black src tests
ruff check src tests
mypy src
```

## License

MIT
