# agent-comms

IRC for agents plus humans.

A coordination wire where agent threads and human threads join the same
channels, see the same messages, and fork the same way. One shared bus, one
registry, one ledger — every client (pi, Toad, VS Code, plain CLI) is a thin
adapter over the same Python core.

Every type owns exactly one concept's semantics. Instantiating a type declares
the concept: constructing a [`Thread`][agent_comms.threads.Thread] declares a thread;
constructing a [`Message`][agent_comms.messages.Message] declares a message. Required
relations are proved at construction time and at every operation boundary.
Unknown references raise — the system is fail-closed.

## Components

- **Core** (`agent_comms.comms` and its state-owning components) — threads,
  messages, registry, JSONL bus, shared ledger.
- **CLI** (`agent-comms`) — JSON over stdout; the adapter surface for the pi
  extension and other process-based clients.
- **ACP server** (`agent-comms-acp`) — Agent Client Protocol agent over
  stdio, included in the base package so Toad, Zed, and VS Code connect natively.
- **Toad UI** — separate ACP client for humans; its dependency pin and feature
  tests are maintained in the Toad fork, not in this core package.
- **Pi extension** (`extensions/pi-agent-comms/`) — thin TypeScript shim that
  exposes `comms_send`, `comms_inbox`, `comms_threads`, and `comms_fork` as
  tool calls backed by the CLI.

## Quick start

Install the core CLI and library from PyPI:

```bash
pip install agent-comms
```

The base package includes the ACP dependency used by Core's runtime and errors;
no extra is needed for `agent-comms install` or graphical clients such as Toad.

For a reproducible local installation with the merged Toad and Textual forks,
use the [pinned stack](stack/README.md). The stack fixes the core and both
external forks to reviewed commits.

In an ACP client, normal prompts run the configured coding agent and stream
thinking and tool progress. Use `@name message`, `#channel message`, or
`!relay message` for coordination-only messages that should not launch a
coding turn.

```python
from pathlib import Path
from agent_comms.threads import Thread
from agent_comms.comms import wire

comms = wire(Path("~/.agent-comms").expanduser())
comms.registry.declare(Thread(name="PR111", tags=frozenset({"base"}), worktree=str(Path("~/wt/pr111").expanduser())))
comms.messaging.broadcast("PR111", "CI is green")
comms.bus.inbox("fixer")
```

Humans join the same wire — register a thread with your name and read the
inbox from the CLI or connect a separately maintained Toad ACP client:

```bash
agent-comms --root ~/.agent-comms register --name tristan --worktree ~/code
agent-comms --root ~/.agent-comms inbox --thread tristan
```

Fail-closed:

```python
comms.registry.require("nonexistent")
# UnregisteredThreadError: Thread 'nonexistent' is not registered.
```

## Development

Install editable before importing from a fresh checkout. The Hatch build hook
writes the checkout's Core commit into package data; runtime imports never run
Git. Source distributions retain that stamp, and Git archives use their export
stamp. Rebuild after changing the checked-out commit to refresh the build identity.

```bash
git clone --recurse-submodules https://github.com/OpenHCSDev/agent-comms.git
cd agent-comms
pip install -e src/agent_comms/skills/refactor-audit -e ".[dev]"
python -m pytest
black src tests
ruff check src tests
mypy src
```

The refactor-audit skill is pinned as a Git submodule under
`src/agent_comms/skills/`. For an existing checkout, run
`git submodule update --init --recursive` before installing or building.
`agent-comms install` discovers every skill in that directory and links it into
each detected harness; refactor-audit needs no separate install path or copy.
Skill edits belong in [OpenHCSDev/refactor-audit](https://github.com/OpenHCSDev/refactor-audit),
then the submodule pin is advanced here. The development install above also
uses that same checkout for the shared audit measures.

Tests use the native package of the checkout's own pin, `stack/.pi-native-<pin>/`,
never the installed one. When it is missing, the pytest session runs
`stack/bin/prepare-pi-native` before any worker starts, so a changed pin rebuilds
on the next run.

Pytest uses separate `pytest-xdist` worker processes (automatic CPU detection,
capped at eight) with work stealing and combined coverage. Use `pytest -n 0`
for serial debugging or `pytest -n 4` to choose a worker count.

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
