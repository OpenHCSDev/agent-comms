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
  stdio, so Toad, Zed, and VS Code connect natively (`agent-comms[acp]`).
- **TUI** (`agent_comms.tui`) — Textual overview client for humans. Optional
  dep (`agent-comms[tui]`).
- **Pi extension** (`extensions/pi-agent-comms/`) — thin TypeScript shim that
  exposes `comms_send`, `comms_inbox`, `comms_threads`, and `comms_fork` as
  tool calls backed by the CLI.

## Quick start

Install the core CLI and library from PyPI:

```bash
pip install agent-comms
```

Install the optional ACP server and Textual TUI dependencies together:

```bash
pip install "agent-comms[all]"
```

In an ACP client, normal prompts run the configured coding agent and stream
thinking and tool progress. Use `@name message`, `#channel message`, or
`!relay message` for coordination-only messages that should not launch a
coding turn.

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

## Releasing

Distribution artifacts are built and validated automatically when a GitHub
release is published. Publishing uses PyPI Trusted Publishing through the
`pypi` GitHub environment; no long-lived API token is stored in the repository.

Before the first release, configure a pending PyPI trusted publisher for:

- Owner: `OpenHCSDev`
- Repository: `agent-comms`
- Workflow: `publish-to-pypi.yml`
- Environment: `pypi`

Set the version in `src/agent_comms/__init__.py`, run the development checks,
and publish a GitHub release for the matching tag.

## License

MIT
